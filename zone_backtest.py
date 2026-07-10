"""Backtest zone-DvP x shot-profile for POINTS — the granular matchup the aggregate DvP missed.

For a scorer, expected points shift = SUM over zones of (their attempts/game in zone z) x
(opponent's FG% allowed in zone z minus league) x (2 or 3). A 3-heavy guard vs a team that
walls the arc gets a real negative; a paint scorer vs a rim-soft team a real positive. This is
strictly more specific than one per-position DvP coefficient.

Leak-free: league zone FG%, each team's zone defense, and each player's shot distribution are
all built from games BEFORE the test window; minutes-honest points from prior games.

    python zone_backtest.py [days]
"""
import math
import statistics as st
import sys
from collections import defaultdict

import gs_backtest as G
import wnba_backtest_layers as B
import wnba_dvp as D
import wnba_pbp as P

ZV = {"rim": 2, "paint": 2, "midrange": 2, "long2": 2, "corner3": 3, "abovebreak3": 3}


def zone(x, y, sv, text):
    if "three" in text.lower() or sv == 3:
        return "corner3" if y < 7.5 else "abovebreak3"
    d = math.hypot(x - 25, y)
    return "rim" if d < 4 else "paint" if d < 8 else "midrange" if d < 16 else "long2"


def game_shots(gid):
    j = P.fetch(gid)
    teams = {(tm.get("team") or {}).get("id"): (tm.get("team") or {}).get("abbreviation")
             for tm in j.get("boxscore", {}).get("teams", [])}
    ids = [i for i in teams if i]
    out = []
    if len(ids) != 2:
        return out
    for pl in j.get("plays", []):
        if not pl.get("shootingPlay"):
            continue
        c = pl.get("coordinate", {}) or {}
        x, y = c.get("x", -2e9), c.get("y", -2e9)
        if x < -1e9 or "free throw" in (pl.get("text", "") or "").lower():
            continue
        stid = (pl.get("team") or {}).get("id")
        parts = pl.get("participants") or []
        pid = (parts[0].get("athlete") or {}).get("id") if parts else None
        if stid not in teams or not pid:
            continue
        out.append({"pid": pid, "def": teams[ids[0] if ids[1] == stid else ids[1]],
                    "zone": zone(x, y, pl.get("scoreValue"), pl.get("text", "")),
                    "made": bool(pl.get("scoringPlay"))})
    return out


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    _id2pos, wnba = D.positions()
    hist, shots = defaultdict(list), []
    for gid, date in B.game_ids(days + 24):
        try:
            for pid, team, opp, poss, r in D._boxscore(gid):
                if team in wnba and opp in wnba:
                    hist[pid].append({**r, "date": date, "team": team, "opp": opp})
            for s in game_shots(gid):
                s["date"] = date
                shots.append(s)
        except Exception:
            continue
    for pid in hist:
        hist[pid].sort(key=lambda g: g["date"])
    cutoff = (B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
              - B.dt.timedelta(days=days)).strftime("%Y-%m-%d")
    tr = [s for s in shots if s["date"] < cutoff and s["def"] in wnba]

    lg = defaultdict(lambda: [0, 0])
    for s in tr:
        z = lg[s["zone"]]
        z[0] += 1
        z[1] += s["made"]
    lgfg = {z: (v[1] / v[0] if v[0] else 0) for z, v in lg.items()}
    td = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for s in tr:
        z = td[s["def"]][s["zone"]]
        z[0] += 1
        z[1] += s["made"]
    pgames, patt = defaultdict(set), defaultdict(lambda: defaultdict(int))
    for s in tr:
        patt[s["pid"]][s["zone"]] += 1
        pgames[s["pid"]].add(s["date"])

    def tdelta(team, z):
        a, m = td[team][z]
        return max(-0.15, min(0.15, m / a - lgfg.get(z, 0))) if a >= 5 else 0.0

    def attpg(pid, z):
        g = len(pgames.get(pid, ()))
        return patt[pid][z] / g if g else 0.0

    base = {"n": 0, "over": 0}
    ov = {m: {"n": 0, "w": 0} for m in ("MH", "ZONE")}
    un = {m: {"n": 0, "w": 0} for m in ("MH", "ZONE")}
    err = {m: [] for m in ("MH", "ZONE")}
    adj = []
    for pid, games in hist.items():
        for i, g in enumerate(games):
            if g["date"] < cutoff:
                continue
            prior = games[:i]
            if len(prior) < 5:
                continue
            proj_min = st.mean(x["min"] for x in prior[-5:])
            mh = G.elev_proj(prior, proj_min, "pts")
            if mh is None:
                continue
            za = sum(attpg(pid, z) * tdelta(g["opp"], z) * ZV[z] for z in ZV)
            zx = mh + za
            adj.append(za)
            a = g["pts"]
            L = B.math.floor(st.mean(x["pts"] for x in prior)) + 0.5
            base["n"] += 1
            base["over"] += 1 if a > L else 0
            err["MH"].append(abs(mh - a))
            err["ZONE"].append(abs(zx - a))
            for m, pr in (("MH", mh), ("ZONE", zx)):
                if pr >= L + 0.5:
                    ov[m]["n"] += 1
                    ov[m]["w"] += 1 if a > L else 0
                elif pr <= L - 0.5:
                    un[m]["n"] += 1
                    un[m]["w"] += 1 if a < L else 0

    bo = 100 * base["over"] / base["n"] if base["n"] else 0
    bu = 100 - bo
    print(f"\nZONE-DvP x SHOT-PROFILE BACKTEST (points) — last {days} days, {base['n']} spots "
          f"(blind base over {bo:.1f}% / under {bu:.1f}%)")
    print(f"zone adjustment: mean {st.mean(adj):+.2f} pts, range [{min(adj):+.1f}, {max(adj):+.1f}], "
          f"|adj|>1pt on {100*sum(1 for a in adj if abs(a) > 1)//max(len(adj),1)}% of spots\n")
    print(f"  {'method':7}{'MAE':>7}{'over hit (vs base)':>22}{'under hit (vs base)':>22}")
    for m in ("MH", "ZONE"):
        orat = 100 * ov[m]["w"] / ov[m]["n"] if ov[m]["n"] else float("nan")
        urat = 100 * un[m]["w"] / un[m]["n"] if un[m]["n"] else float("nan")
        mae = st.mean(err[m]) if err[m] else 0
        print(f"  {m:7}{mae:>7.2f}{orat:>12.1f}% ({orat-bo:+.1f}) n{ov[m]['n']:<5}"
              f"{urat:>10.1f}% ({urat-bu:+.1f}) n{un[m]['n']}")


if __name__ == "__main__":
    main()
