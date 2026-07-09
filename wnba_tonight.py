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


def prop_edges(player, log, proj_min):
    """For each posted OVER prop, the player's hit rate in ELEVATED-ROLE games
    (min >= proj_min - 4) vs the posted price → flag +EV ladders. The raw hit rate on
    ~5-10 games is noise, and the book set the line KNOWING the role, so we shrink our
    estimate toward the book's implied prob by sample size (k=6) before scoring EV —
    a thin-sample spot must be extreme to survive. Returns raw hit + n for judgment."""
    elevated = [g for g in log if g["min"] >= proj_min - 4]
    if len(elevated) < 4:
        return []
    out = []
    for stat, best in posted_props(player).items():
        key = PROP_STATS[stat]
        vals = [g[key] for g in elevated]
        n = len(vals)
        for line, dec in sorted(best.items()):
            hit = sum(1 for v in vals if v > line) / n
            implied = 1 / dec
            p_adj = (hit * n + implied * 6) / (n + 6)      # credibility shrink to the line
            ev = p_adj * dec - 1
            if ev >= 0.05:
                out.append((ev, stat, line, dec, hit, n))
    return sorted(out, reverse=True)

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
EH = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# ESPN abbrev -> stats.nba abbrev (only where they differ)
TEAM_FIX = {"GS": "GSV", "LA": "LAS", "CONN": "CON", "WSH": "WAS", "NY": "NYL",
            "LV": "LVA", "PHO": "PHX"}


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
                blog = W.game_log(v["id"])
                print(f"  {n:22} → ~{proj_min:.0f}min ({dmin:+.0f}), {dpts:+.1f}pts w/o")
                for ev, stat, line, dec, hit, ns in prop_edges(n, blog, proj_min):
                    print(f"       ✅ {stat} over {line:g} @ {_am(dec)} — hit {hit*100:.0f}% "
                          f"in {ns} elevated-role games (+{ev*100:.0f}% est EV)")
        except RuntimeError:
            print("  (stats fetch failed, retry)")
        print()


if __name__ == "__main__":
    main()
