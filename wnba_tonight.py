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

import requests

import wnba_wowy as W

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
EH = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# ESPN abbrev -> stats.nba abbrev (only where they differ)
TEAM_FIX = {"GS": "GSV", "LA": "LAS", "CONN": "CON", "WSH": "WAS", "NY": "NYL",
            "LV": "LVA", "PHO": "PHX"}


def _espn(path):
    r = requests.get(f"{ESPN}/{path}", headers=EH, timeout=20)
    return r.json() if r.status_code == 200 else {}


def tonight_teams():
    """{stats.nba abbrev} of teams with a game today (not final)."""
    out = set()
    for e in _espn("scoreboard").get("events", []):
        st = e.get("status", {}).get("type", {}).get("state")
        if st == "post":
            continue                       # already final
        for c in e.get("competitions", [{}])[0].get("competitors", []):
            ab = c.get("team", {}).get("abbreviation", "")
            out.add(TEAM_FIX.get(ab, ab))
    return out


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
    playing = tonight_teams()
    inj = injuries()
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
        print(f"=== {name} ({p['team']}) {status} — {p['min']:.0f} mpg, {p['pts']:.0f} ppg "
              f"vacated ===")
        try:
            tlog = W.game_log(p["id"])
            team_pl = {n: v for n, v in pl.items()
                       if v["team"] == p["team"] and n != name and v["gp"] >= 5}
            rows = []
            for n, v in team_pl.items():
                w = W.wowy(W.game_log(v["id"]), tlog)
                if w["n_without"] >= 2:
                    dmin = w["without"]["min"]["mean"] - w["with"]["min"]["mean"]
                    dpts = w["without"]["pts"]["mean"] - w["with"]["pts"]["mean"]
                    rows.append((dmin, dpts, n, v, w))
            for dmin, dpts, n, v, w in sorted(rows, reverse=True)[:4]:
                proj_min = w["without"]["min"]["mean"]
                # production in the minutes band the beneficiary now projects into
                bands = W.minutes_bands(W.game_log(v["id"]))
                key = f"{int(proj_min//4)*4}-{int(proj_min//4)*4+4}"
                band = bands.get(key, {}).get("pts", {})
                seen = f"pts in {key}min games: {band.get('vals')}" if band else "thin band"
                print(f"  {n:22} → ~{proj_min:.0f}min ({dmin:+.0f}), {dpts:+.1f}pts w/o | {seen}")
        except RuntimeError:
            print("  (stats fetch failed, retry)")
        print()


if __name__ == "__main__":
    main()
