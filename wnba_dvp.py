"""WNBA matchup / defense context — the third leg (opponent production allowed + pace).

Position-level defense-vs-position is the ideal, but stats.nba's position/opponent-split
endpoints are heavily rate-limited (they time out after a dev machine makes many calls).
So this uses the ONE light, reliable call — team opponent stats allowed — to answer the
core matchup question: is tonight's opponent stingy or leaky, and fast or slow? Cached to
wnba_dvp.json and refreshed from CI's un-throttled IP.

    python wnba_dvp.py --refresh     # rebuild the cache (run in CI)
    python wnba_dvp.py               # print the matchup table
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CACHE = HERE / "wnba_dvp.json"
API = "https://stats.nba.com/stats"
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
     "Referer": "https://www.wnba.com/", "Origin": "https://www.wnba.com",
     "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
     "Accept": "application/json"}
SEASON = "2026"


def _get(ep, **p):
    p.setdefault("LeagueID", "10")
    for a in range(4):
        try:
            r = requests.get(f"{API}/{ep}", params=p, headers=H, timeout=45)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(3 * (a + 1))
    return None


def refresh():
    """{team_abbr: {'opp_pts','opp_reb','opp_ast','pace','pts_rank'}} vs league avg=1.0."""
    j = _get("leaguedashteamstats", Season=SEASON, SeasonType="Regular Season",
             PerMode="PerGame", MeasureType="Opponent", PORound="0", Month="0",
             Period="0", LastNGames="0", TeamID="0", Outcome="", Location="",
             SeasonSegment="", DateFrom="", DateTo="", VsConference="", VsDivision="",
             GameSegment="", Conference="", Division="", GameScope="",
             PlayerExperience="", PlayerPosition="", StarterBench="", TwoWay="0")
    if not j or "resultSets" not in j:
        return None
    hdr = j["resultSets"][0]["headers"]
    idx = {h: i for i, h in enumerate(hdr)}
    rows = j["resultSets"][0]["rowSet"]

    def col(name):
        for c in (name, "OPP_" + name.split("_")[-1]):
            if c in idx:
                return idx[c]
        return None

    pk, rk, ak = col("OPP_PTS"), col("OPP_REB"), col("OPP_AST")
    if pk is None:
        return None
    lg_pts = sum(r[pk] for r in rows) / len(rows)
    order = sorted(rows, key=lambda r: -r[pk])          # most points allowed first
    rank = {r[idx["TEAM_ABBREVIATION"]]: i + 1 for i, r in enumerate(order)}
    out = {}
    for r in rows:
        ab = r[idx["TEAM_ABBREVIATION"]]
        out[ab] = {"opp_pts": round(r[pk], 1),
                   "pts_factor": round(r[pk] / lg_pts, 3),
                   "opp_reb": round(r[rk], 1) if rk is not None else None,
                   "opp_ast": round(r[ak], 1) if ak is not None else None,
                   "pts_rank": rank[ab]}
    CACHE.write_text(json.dumps(out, indent=1))
    return out


def load():
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except ValueError:
            pass
    return {}


def matchup_note(opp_abbr):
    """One-line matchup context for tonight's opponent, or '' if no cache."""
    d = load().get(opp_abbr)
    if not d:
        return ""
    tag = ("leaky D" if d["pts_factor"] >= 1.04 else
           "stingy D" if d["pts_factor"] <= 0.96 else "avg D")
    return f"vs {opp_abbr}: {d['opp_pts']} pts allowed (#{d['pts_rank']}, {tag})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    if args.refresh:
        d = refresh()
        print(f"dvp cache refreshed: {len(d)} teams" if d else "dvp refresh failed (rate-limited)")
        return
    d = load()
    if not d:
        print("no cache — run --refresh (works from CI)")
        return
    for ab, v in sorted(d.items(), key=lambda x: -x[1]["pts_factor"]):
        print(f"  {ab:4} {v['opp_pts']:5.1f} pts allowed  x{v['pts_factor']:.2f}  #{v['pts_rank']}")


if __name__ == "__main__":
    main()
