"""Symmetric to the injury-OVER edge: when a key player RETURNS after missing a few games, the
beneficiary who inherited his role reverts — but the book's line still anchors on the beneficiary's
recent (elevated) games. So the UNDER is stale-high.

Leak-free test. For each team game G: a key teammate X (>= IMPACT mpg) either RETURNS (missed >=2 of
the last 3 team games, plays G) or is STILL OUT. For each beneficiary Y who is currently ELEVATED
(mean of last 3 games > their earlier baseline) and plays G, bet the UNDER at Y's recent-3 average
(the stale anchor a lagging book posts). Does Y come under?

  X RETURNS  -> role contracts -> Y reverts -> UNDER should hit  (the edge)
  X STILL OUT-> role persists   -> Y stays up -> UNDER ~coinflip  (the control)

The gap between the two is the signal. Reported without MAE.

    python return_backtest.py [min_impact_mpg=18]
"""
import statistics as st
import sys
from collections import defaultdict

import wnba_wowy as W

IMPACT = float(sys.argv[1]) if len(sys.argv) > 1 else 18.0
STATS = {"pts": "points", "reb": "rebounds", "ast": "assists", "pra": "PRA"}


def main():
    pl = W.players()
    logs, byteam = {}, defaultdict(list)
    for n, v in pl.items():
        if v["gp"] < 5:
            continue
        try:
            lg = sorted(W.game_log(v["id"]), key=lambda g: g["date"])
        except Exception:
            continue
        if lg:
            logs[n] = lg
            byteam[v["team"]].append(n)
    gdate = {g["game_id"]: g["date"][:10] for lg in logs.values() for g in lg}

    def played(n, gid):
        return any(g["game_id"] == gid for g in logs.get(n, []))

    res = defaultdict(lambda: defaultdict(list))     # category -> stat -> [under_hit]
    for team, names in byteam.items():
        impact = [n for n in names if pl[n]["min"] >= IMPACT]
        gids = sorted({g["game_id"] for n in names for g in logs[n]}, key=lambda x: gdate.get(x, ""))
        for X in impact:
            xin = {g: played(X, g) for g in gids}
            for i, gid in enumerate(gids):
                d = gdate[gid]
                prior = gids[max(0, i - 3):i]
                if len(prior) < 2 or not any(g in xin for g in prior):
                    continue
                x_out_recent = sum(1 for g in prior if not xin.get(g, True))
                if x_out_recent < 2:                 # X wasn't out the last couple -> not a spot
                    continue
                category = "X RETURNS" if xin.get(gid, False) else "X STILL OUT"
                for Y in names:
                    if Y == X or not played(Y, gid):
                        continue
                    ylog = [g for g in logs[Y] if g["date"][:10] < d]
                    if len(ylog) < 6:
                        continue
                    last3, base = ylog[-3:], ylog[:-3]
                    if len(base) < 3:
                        continue
                    actual = next(g for g in logs[Y] if g["game_id"] == gid)
                    if actual["min"] < 12:
                        continue
                    for sk in STATS:
                        recent_line = st.mean(g[sk] for g in last3)   # stale anchor (elevated)
                        baseline = st.mean(g[sk] for g in base)
                        if recent_line <= baseline + 0.5:             # Y not actually elevated -> skip
                            continue
                        res[category][sk].append(1 if actual[sk] < recent_line else 0)

    print(f"\nRETURN-FROM-INJURY UNDER backtest — bet Y's UNDER at their stale recent-3 line "
          f"(key teammate >= {IMPACT:g}mpg), leak-free\n")
    for cat in ("X RETURNS", "X STILL OUT"):
        print(f"{cat}:")
        for sk, full in STATS.items():
            h = res[cat][sk]
            if h:
                print(f"  {full:8} UNDER hits {sum(h)/len(h)*100:4.0f}%  n={len(h)}")
        print()
    # the signal = how much MORE the under hits when X returns vs stays out
    print("edge (return minus still-out, under-hit %):")
    for sk, full in STATS.items():
        r, s = res["X RETURNS"][sk], res["X STILL OUT"][sk]
        if r and s:
            print(f"  {full:8} {sum(r)/len(r)*100 - sum(s)/len(s)*100:+.0f} pts")


if __name__ == "__main__":
    main()
