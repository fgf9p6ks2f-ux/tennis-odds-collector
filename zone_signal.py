"""Is there REAL zone-matchup signal? (answering: the first backtest was underpowered.)

Fixes: (1) ROLLING season-long zone-defense — each game uses ALL prior games, not a stale 24-day
window; (2) a POWER-adequate metric — correlate the zone adjustment with the projection's actual
error (continuous), not a binary hit on 550 spots; (3) estimate the OPTIMAL SHRINKAGE beta (OLS
of residual on zone_adj) — applying a noisy signal at full strength is how you add error even
when signal exists. If beta>0 with a real t-stat, the signal is real and a SHRUNK adjustment
should help; then we re-check the bet with beta*adj.

Leak-free: chronological pass; each game scored on state built strictly from earlier games.

    python zone_signal.py [test_days] [warmup_days]
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
                    "zone": zone(x, y, pl.get("scoreValue"), pl.get("text", "")), "made": bool(pl.get("scoringPlay"))})
    return out


def main():
    test_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    warmup_days = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    _id2pos, wnba = D.positions()
    now = B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
    warm = (now - B.dt.timedelta(days=test_days + warmup_days)).strftime("%Y-%m-%d")
    tstart = (now - B.dt.timedelta(days=test_days)).strftime("%Y-%m-%d")

    games = sorted(B.game_ids(test_days + warmup_days + 4), key=lambda gd: gd[1])
    hist = defaultdict(list)                       # pid -> prior box games
    lgz = defaultdict(lambda: [0, 0])              # rolling league zone [fga, fgm]
    tdz = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # team -> zone -> [fga, fgm]
    patt = defaultdict(lambda: defaultdict(int))  # pid -> zone -> attempts
    pgm = defaultdict(set)                         # pid -> game dates

    def tdelta(team, z):
        a, m = tdz[team][z]
        lf = lgz[z][1] / lgz[z][0] if lgz[z][0] else 0
        return max(-0.15, min(0.15, m / a - lf)) if a >= 15 else 0.0

    pairs = []                                     # (residual, zone_adj)
    for gid, date in games:
        try:
            box = {pid: (team, opp, r) for pid, team, opp, poss, r in D._boxscore(gid)}
            shots = game_shots(gid)
        except Exception:
            continue
        # SCORE first (state is strictly pre-game)
        if date >= tstart:
            for pid, (team, opp, r) in box.items():
                if team not in wnba or opp not in wnba or len(hist[pid]) < 5:
                    continue
                proj_min = st.mean(x["min"] for x in hist[pid][-5:])
                mh = G.elev_proj(hist[pid], proj_min, "pts")
                if mh is None:
                    continue
                ng = len(pgm[pid]) or 1
                za = sum((patt[pid][z] / ng) * tdelta(opp, z) * ZV[z] for z in ZV)
                pairs.append((r["pts"] - mh, za))
        # THEN update state with this game
        for pid, (team, opp, r) in box.items():
            hist[pid].append(r)
        for s in shots:
            if s["def"] in wnba:
                tdz[s["def"]][s["zone"]][0] += 1
                tdz[s["def"]][s["zone"]][1] += s["made"]
            lgz[s["zone"]][0] += 1
            lgz[s["zone"]][1] += s["made"]
            patt[s["pid"]][s["zone"]] += 1
            pgm[s["pid"]].add(date)

    n = len(pairs)
    if n < 30:
        print(f"only {n} pairs — not enough"); return
    ys = [p[0] for p in pairs]                     # residual
    xs = [p[1] for p in pairs]                     # zone_adj
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    r = cov / (vx * vy) ** 0.5 if vx and vy else 0
    beta = cov / vx if vx else 0                   # OLS: residual = beta * zone_adj
    t = r * ((n - 2) / (1 - r * r)) ** 0.5 if abs(r) < 1 else 0
    print(f"\nZONE-SIGNAL TEST — {n} points-spots, rolling season-long zone defense\n")
    print(f"  does zone_adj predict the projection's ERROR (actual - MH)?")
    print(f"    correlation r = {r:+.3f}   (t = {t:+.2f}, |t|>2 = real signal at n={n})")
    print(f"    OLS beta      = {beta:+.2f}   (optimal weight to APPLY the zone_adj; 0 = useless, "
          f"1 = trust fully)")
    print(f"    zone_adj spread: sd {st.pstdev(xs):.2f} pts, "
          f"|adj|>1 on {100*sum(1 for x in xs if abs(x) > 1)//n}% of spots")
    if t > 2:
        print(f"\n  -> REAL signal. Applying it at beta={beta:.2f} (shrunk) would cut points-MAE; my "
              f"first test applied it at 1.0 (full) which over-trusted the noise. Worth shipping "
              f"a beta-weighted zone term.")
    elif t < -2:
        print(f"\n  -> zone_adj predicts error BACKWARDS (t{t:.1f}) — genuinely counterproductive.")
    else:
        print(f"\n  -> no detectable signal even with more data + the powerful metric (|t|={abs(t):.1f}"
              f"<2). Not just underpowered — the effect is ~0 on what WNBA data exists so far.")


if __name__ == "__main__":
    main()
