"""Show / push tonight's QUESTIONABLE-TIER WATCHLIST — the provisional "if he sits" beneficiary
spots, mirroring exactly what wnba_alert surfaces (same firm-out set + questionable_beneficiaries),
with the lead-time tell per star.

    python show_watchlist.py            # print the board
    python show_watchlist.py --push     # also push it to ntfy (NTFY_TOPIC) if there's anything to show

The --push path is how the CLOUD delivers the consolidated board to the phone (wnba-watchlist-digest
workflow) — no local machine required.
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict

import requests

import wnba_context as CTX
import wnba_tonight as T
import wnba_wowy as W


_ABBR = {"points": "pts", "rebounds": "reb", "assists": "ast", "pra": "PRA",
         "pts_reb": "P+R", "pts_ast": "P+A", "reb_ast": "R+A"}

# --test payload: a realistic SAMPLE board to verify the ntfy delivery path when no slate is live.
_SAMPLE_QS = {"NY": [("Leonie Fiebich", "Questionable", {"min": 30}, 0.80, 2.0),
                     ("Nyara Sabally", "GTD", {"min": 22}, 0.50, 7.0)]}
_SAMPLE_SPOTS = [
    {"star": "Fiebich", "status": "Questionable", "sit": 0.80, "lead": 2.0,
     "player": "Sabrina Ionescu", "stat": "points", "line": 21.5, "ev": 0.12,
     "elev_avg": 24.1, "hit": 0.6, "n": 10, "dec": 1.9, "conf": "likely"},
    {"star": "Fiebich", "status": "Questionable", "sit": 0.80, "lead": 2.0,
     "player": "Sabrina Ionescu", "stat": "assists", "line": 6.5, "ev": 0.09,
     "elev_avg": 7.8, "hit": 0.6, "n": 10, "dec": 1.87, "conf": "likely"}]


def _short(name):
    p = name.split()
    return f"{p[0][0]}. {p[-1]}" if len(p) > 1 else name


def _lead_tag(lead):
    if lead is None:
        return ""
    return f" · Q'd {lead:.0f}h pre-tip" + (" LATE" if lead < T.LEAD_SPLIT else "")


def gather():
    """(sorted playing teams, questionable_stars dict, watchlist spots) — ([], {}, []) if no slate."""
    pl = W.players()
    playing = T.tonight_teams()
    if not playing:
        return [], {}, []
    matchups, inj = T.tonight_matchups(), T.injuries()
    lines, rates = CTX.game_lines(), CTX.team_rates()
    out_names = {n for n, s in inj.items() if s in ("Out", "Doubtful") and not T.playing_now(n)}
    firm_by_team = defaultdict(list)
    for name, status in inj.items():
        p = pl.get(name)
        if (p and p["team"] in playing and status in ("Out", "Doubtful")
                and (p["min"] >= 20 or p["pts"] >= 10) and not T.playing_now(name)):
            firm_by_team[p["team"]].append((name, p))
    qs = T.questionable_stars(pl, playing, inj, out_names)
    spots = T.questionable_beneficiaries(pl, playing, matchups, lines, rates, inj,
                                         out_names, firm_by_team)
    return sorted(playing), qs, spots


def print_console(playing, qs, spots):
    if not playing:
        print("No live/upcoming WNBA slate — the watchlist is empty until the next slate is pending.")
        return
    print(f"Slate: {', '.join(playing)}")
    n_stars = sum(len(v) for v in qs.values())
    if not n_stars:
        print("No key players tagged QUESTIONABLE / GTD yet — nothing to watch "
              "(the 60s watcher pushes the moment one is tagged).")
        return
    print(f"\nQUESTIONABLE stars ({n_stars}):")
    for team, stars in sorted(qs.items()):
        for name, status, p, sp, lead in sorted(stars, key=lambda x: -x[3]):
            print(f"  {name} ({team}) {status} — {p['min']:.0f}mpg — "
                  f"~{sp * 100:.0f}% to sit{_lead_tag(lead)}")
    print(f"\nWATCHLIST — 'if he sits' overs ({len(spots)}):")
    for s in sorted(spots, key=lambda s: -(s.get("ev") or 0)):
        hits = round((s.get("hit") or 0) * (s.get("n") or 0))
        print(f"  if {s['star']} sits -> {s['player']} {s['stat']} o{s['line']:g} @ {T._am(s['dec'])} "
              f"+{s['ev'] * 100:.0f}%EV · proj {s['elev_avg']:g} · {hits}/{s['n']} · "
              f"~{s['sit'] * 100:.0f}% sit{_lead_tag(s.get('lead'))} [{s['conf']}]")


def push_body(qs, spots):
    """Compact scannable ntfy body: the 'if he sits' overs grouped by star (with lead-time), plus a
    tail line for questionable stars that produced no +EV spot."""
    by_star = defaultdict(list)
    for s in sorted(spots, key=lambda s: -(s.get("ev") or 0)):
        by_star[(s["star"], s["status"], s["sit"], s.get("lead"))].append(s)
    blocks, spot_last = [], set()
    for (star, status, sit, lead), ss in by_star.items():
        spot_last.update(star.split("+"))
        hdr = f"⏳ if {star} ({status} · ~{sit * 100:.0f}% sit{_lead_tag(lead)}) sits:"
        bullets = "\n".join(f"• {_short(s['player'])} {_ABBR.get(s['stat'], s['stat'])} o{s['line']:g} "
                            f"+{s['ev'] * 100:.0f}%EV" for s in ss)
        blocks.append(hdr + "\n" + bullets)
    for team, stars in sorted(qs.items()):
        for name, status, p, sp, lead in stars:
            if name.split()[-1] not in spot_last:
                blocks.append(f"⏳ {_short(name)} ({team}) {status} · ~{sp * 100:.0f}% "
                              f"sit{_lead_tag(lead)} — no +EV spot")
    return "\n\n".join(blocks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="post the board to ntfy (NTFY_TOPIC)")
    ap.add_argument("--test", action="store_true",
                    help="push a clearly-marked SAMPLE board to verify delivery (ignores the slate)")
    a = ap.parse_args()
    if a.test:
        qs, spots = _SAMPLE_QS, _SAMPLE_SPOTS
        print_console(["NY", "SEA"], qs, spots)
    else:
        playing, qs, spots = gather()
        print_console(playing, qs, spots)
    if not a.push:
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("push: NTFY_TOPIC not set")
        return
    if not sum(len(v) for v in qs.values()):
        print("push: nothing to push (no questionable stars)")     # never spam an empty board
        return
    body = push_body(qs, spots)
    title = "WNBA watchlist (questionable)"
    if a.test:
        body = "[TEST] sample board — confirming the digest push reaches your phone.\n\n" + body
        title = "WNBA watchlist [TEST]"
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers={"Title": title, "Priority": "default",
                               "Tags": "hourglass_flowing_sand"}, timeout=15)
        print("push: sent")
    except requests.RequestException as e:
        print("push failed:", e)


if __name__ == "__main__":
    main()
