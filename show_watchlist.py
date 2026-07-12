"""Print tonight's QUESTIONABLE-TIER WATCHLIST — the provisional "if he sits" beneficiary spots,
mirroring exactly what wnba_alert.collect() surfaces (same firm-out set + questionable_beneficiaries),
with the lead-time tell on each questionable star. A read-only snapshot for a manual / scheduled look.

    python show_watchlist.py
"""
from __future__ import annotations

from collections import defaultdict

import wnba_context as CTX
import wnba_tonight as T
import wnba_wowy as W


def main():
    pl = W.players()
    playing = T.tonight_teams()
    matchups = T.tonight_matchups()
    inj = T.injuries()
    if not playing:
        print("No live/upcoming WNBA slate right now — the watchlist is slate-driven, so it's empty "
              "until the next slate's games are pending.")
        return
    print(f"Slate: {', '.join(sorted(playing))}   ({len(inj)} injury-listed)")

    lines, rates = CTX.game_lines(), CTX.team_rates()
    # firm outs = the exact set wnba_alert bets off of
    out_names = {n for n, s in inj.items() if s in ("Out", "Doubtful") and not T.playing_now(n)}
    firm_by_team = defaultdict(list)
    for name, status in inj.items():
        p = pl.get(name)
        if (p and p["team"] in playing and status in ("Out", "Doubtful")
                and (p["min"] >= 20 or p["pts"] >= 10) and not T.playing_now(name)):
            firm_by_team[p["team"]].append((name, p))

    qs = T.questionable_stars(pl, playing, inj, out_names)
    n_stars = sum(len(v) for v in qs.values())
    if not n_stars:
        print("\nNo key players tagged QUESTIONABLE / GTD on the slate yet — nothing to watch.\n"
              "(The 60s watcher fires a push the moment one is tagged; questionables firm up over the "
              "hours before tip.)")
        return

    print(f"\nQUESTIONABLE stars ({n_stars}):")
    for team, stars in sorted(qs.items()):
        for name, status, p, sp, lead in sorted(stars, key=lambda x: -x[3]):
            lt = (f"tagged {lead:.0f}h pre-tip" + ("  ** LATE **" if lead < T.LEAD_SPLIT else "")
                  if lead is not None else "lead unknown")
            print(f"  {name} ({team}) {status} — {p['min']:.0f} mpg — ~{sp * 100:.0f}% to sit · {lt}")

    spots = T.questionable_beneficiaries(pl, playing, matchups, lines, rates, inj,
                                         out_names, firm_by_team)
    print(f"\nWATCHLIST — provisional 'if he sits' overs ({len(spots)}):")
    if not spots:
        print("  (questionable stars detected, but no +EV over cleared for their beneficiaries)")
        return
    for s in sorted(spots, key=lambda s: -(s.get("ev") or 0)):
        lead = s.get("lead")
        lt = (f" · Q'd {lead:.0f}h pre-tip{' LATE' if lead < T.LEAD_SPLIT else ''}"
              if lead is not None else "")
        hits = round((s.get("hit") or 0) * (s.get("n") or 0))
        print(f"  if {s['star']} ({s['status']}) sits -> {s['player']} {s['stat']} o{s['line']:g} "
              f"@ {T._am(s['dec'])}  +{s['ev'] * 100:.0f}% EV · proj {s['elev_avg']:g} vs "
              f"{s['season_avg']:g} · {hits}/{s['n']} in role · ~{s['sit'] * 100:.0f}% to sit{lt} "
              f"[{s['conf']}]")


if __name__ == "__main__":
    main()
