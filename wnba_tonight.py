"""WNBA tonight board — the TRIGGER. Turns 'who's out' into 'here are the spots'.

Ties tonight's ESPN injury report + schedule to the WOWY engine: for every key player
ruled OUT on a team playing tonight, surface who inherits the minutes/usage and their
production in past games at that role — so the spot finds YOU instead of you memorizing
lineups. This is step 1 of 3 (trigger -> prop-line integration -> DvP).

    python wnba_tonight.py             # tonight's absences + beneficiaries
    python wnba_tonight.py --min-out 22  # only key players (>=22 mpg) being out
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

import requests

import wnba_dvp as DVP
import wnba_wowy as W

PROPS_DB = Path(os.environ.get("FD_DB",
                Path(__file__).resolve().parent / "fanduel_props.sqlite"))
# fd_lines stat keys we can project from a game log
PROP_STATS = {"points": "pts", "rebounds": "reb", "assists": "ast"}


def _am(dec):
    return f"+{round((dec-1)*100)}" if dec >= 2 else f"{round(-100/(dec-1))}"


def posted_props(player):
    """Latest WNBA props for a player: {stat_key: {line: best_over_dec}} across books."""
    if not PROPS_DB.exists():
        return {}
    con = sqlite3.connect(PROPS_DB)
    rows = con.execute(
        "SELECT stat, line, side, odds, COALESCE(book,'fd') FROM fd_lines "
        "WHERE sport='wnba' AND player=? AND collected_at > datetime('now','-1 day')",
        (player,)).fetchall()
    con.close()
    best = defaultdict(dict)
    for stat, line, side, odds, _bk in rows:
        if stat in PROP_STATS and side == "over" and line is not None:
            k = round(float(line), 1)
            best[stat][k] = max(best[stat].get(k, 0), float(odds))
    return best


# Role floor scaled for WNBA's shorter 40-min game (NBA is 48): a bench player promoted
# to the starting lineup projects to ~22+ min, so judge production in their 22+ min games.
ROLE_FLOOR = 22.0


def prop_edges(player, log, proj_min):
    """+EV over-props, framed as the user's actual edge: the gap between ELEVATED-ROLE
    production and a line the book anchored to the SEASON AVERAGE. For each posted line:
    hit rate in the player's elevated games (min >= max(proj-4, 22)), credibility-shrunk
    to the book's implied prob (thin samples + the book set the line), flagged when +EV.
    Each dict also carries season_avg vs elev_avg — the mispricing is fattest when the
    line sits near season_avg but elev_avg clears it. Returns list of dicts."""
    floor = max(proj_min - 4, ROLE_FLOOR)
    elevated = [g for g in log if g["min"] >= floor]
    if len(elevated) < 4:
        return []
    fga = st.mean([g["fga"] for g in elevated])
    out = []
    for stat, best in posted_props(player).items():
        key = PROP_STATS[stat]
        season_avg = st.mean([g[key] for g in log]) if log else 0
        vals = [g[key] for g in elevated]
        elev_avg = st.mean(vals)
        n = len(vals)
        for line, dec in sorted(best.items()):
            # Only the CREDIBLE market. Deep alt rungs (a 20-pt scorer's o4.5) and
            # implausible prices (near-lock at plus money, or a lottery longshot) are
            # alt-ladder/scrape artifacts that manufacture fake EV — drop them. Real prop
            # edges live within a rung or two of the projection at a fair price.
            if line < 0.6 * elev_avg:            # deep rung far below the projection
                continue
            if not (1.25 <= dec <= 3.5):          # ~ -400..+250; kills juiced locks & longshots
                continue
            hit = sum(1 for v in vals if v > line) / n
            p_adj = (hit * n + (1 / dec) * 6) / (n + 6)
            ev = p_adj * dec - 1
            # the user's edge: line anchored near the SEASON avg while the elevated role
            # projects meaningfully higher — the book hasn't repriced the new role.
            stale = elev_avg - season_avg >= 1.0 and line <= (season_avg + elev_avg) / 2
            if ev >= 0.05:
                out.append({"ev": ev, "stat": stat, "line": line, "dec": dec, "hit": hit,
                            "n": n, "fga": fga, "season_avg": round(season_avg, 1),
                            "elev_avg": round(elev_avg, 1), "stale": stale})
    return sorted(out, key=lambda d: -d["ev"])


def double_double_rate(log, proj_min):
    """DD hit rate in the player's elevated-role games — the lagging high-odds market on
    backup bigs (Embiid out → Drummond DD at 2.5-4x). (rate, n) or None if thin."""
    floor = max(proj_min - 4, ROLE_FLOOR - 5)
    elevated = [g for g in log if g["min"] >= floor]
    if len(elevated) < 4:
        return None
    return sum(1 for g in elevated if g["dd"]) / len(elevated), len(elevated)

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
EH = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# players() and the scoreboard are both ESPN now — abbrevs already match, no remap.
TEAM_FIX = {}


def _espn(path):
    r = requests.get(f"{ESPN}/{path}", headers=EH, timeout=20)
    return r.json() if r.status_code == 200 else {}


def tonight_matchups():
    """{stats.nba abbrev: opponent stats.nba abbrev} for tonight's non-final games."""
    out = {}
    for e in _espn("scoreboard").get("events", []):
        if e.get("status", {}).get("type", {}).get("state") == "post":
            continue
        comp = e.get("competitions", [{}])[0].get("competitors", [])
        abs_ = [TEAM_FIX.get(c.get("team", {}).get("abbreviation", ""),
                             c.get("team", {}).get("abbreviation", "")) for c in comp]
        if len(abs_) == 2:
            out[abs_[0]] = abs_[1]
            out[abs_[1]] = abs_[0]
    return out


def tonight_teams():
    return set(tonight_matchups())


def injuries():
    """{player_name: status} for Out / Doubtful / Questionable."""
    out = {}
    for t in _espn("injuries").get("injuries", []):
        for p in t.get("injuries") or []:
            nm = p.get("athlete", {}).get("displayName")
            status = p.get("status")
            if nm and status in ("Out", "Doubtful", "Questionable"):
                out[nm] = status
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-out", type=float, default=20.0,
                    help="only flag absences of players averaging >= this many minutes")
    args = ap.parse_args()

    pl = W.players()
    matchups = tonight_matchups()
    playing = set(matchups)
    inj = injuries()
    out_names = {n for n, s in inj.items() if s in ("Out", "Doubtful")}
    print(f"Tonight: {len(playing)} teams in action · {len(inj)} injury-listed players\n")

    # key OUT players whose team plays tonight
    flagged = []
    for name, status in inj.items():
        p = pl.get(name)
        if not p or p["team"] not in playing or p["min"] < args.min_out:
            continue
        if status not in ("Out", "Doubtful"):     # questionable = watch, not yet actionable
            continue
        flagged.append((name, status, p))
    flagged.sort(key=lambda x: -x[2]["min"])

    if not flagged:
        print("no key players ruled out on tonight's slate yet — check back ~30min pre-tip.")
        return

    for name, status, p in flagged:
        note = DVP.matchup_note(matchups.get(p["team"], ""))
        print(f"=== {name} ({p['team']}) {status} — {p['min']:.0f} mpg, {p['pts']:.0f} ppg "
              f"vacated ===" + (f"  [{note}]" if note else ""))
        try:
            tlog = W.game_log(p["id"])
            team_pl = {n: v for n, v in pl.items()
                       if v["team"] == p["team"] and n != name and v["gp"] >= 5
                       and n not in out_names}
            rows = []
            for n, v in team_pl.items():
                w = W.wowy(W.game_log(v["id"]), tlog)
                if w["n_without"] >= 2:
                    dmin = w["without"]["min"]["mean"] - w["with"]["min"]["mean"]
                    dpts = w["without"]["pts"]["mean"] - w["with"]["pts"]["mean"]
                    rows.append((dmin, dpts, n, v, w))
            for dmin, dpts, n, v, w in sorted(rows, reverse=True)[:4]:
                proj_min = w["without"]["min"]["mean"]
                blog = W.game_log(v["id"])
                edges = prop_edges(n, blog, proj_min)
                fga = edges[0]["fga"] if edges else 0
                print(f"  {n:22} → ~{proj_min:.0f}min ({dmin:+.0f}), {dpts:+.1f}pts w/o"
                      + (f", {fga:.0f} FGA" if fga else ""))
                for e in edges:
                    star = " ⟵ stale line" if e["stale"] else ""
                    print(f"       ✅ {e['stat']} over {e['line']:g} @ {_am(e['dec'])} — "
                          f"elev {e['elev_avg']:g} vs season {e['season_avg']:g}, "
                          f"hit {e['hit']*100:.0f}% in {e['n']} games "
                          f"(+{e['ev']*100:.0f}% EV){star}")
                dd = double_double_rate(blog, proj_min)
                if dd and dd[0] >= 0.35:                 # check the lagging DD market
                    print(f"       ★ double-double {dd[0]*100:.0f}% in {dd[1]} elevated games "
                          f"— check the DD price (often stale/generous for backup bigs)")
        except RuntimeError:
            print("  (stats fetch failed, retry)")
        print()


if __name__ == "__main__":
    main()
