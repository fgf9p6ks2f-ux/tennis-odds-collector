"""Does the INJURY DRIVER separate a real over edge from a coin-flip star-over?

The model flags an over whenever its projection clears the line — even when the injury doesn't
actually expand the beneficiary's role (Stewart o18.5: usage +0.1, minutes -3.5 w/o Fiebich, yet
flagged because her star baseline > the line). The hypothesis: overs only pay when the injury
genuinely lifts usage/minutes; flat-role star-overs regress.

Leak-free test: for each (team, game) where a key teammate (>= IMPACT mpg) was out, for each
beneficiary who played, compute — from games BEFORE that date — the WOWY driver (usage-FGA delta
and minutes delta without the out-set). The 'line' = the player's normal-role baseline (their mean
with the out-set PLAYING). Bet the OVER at that stale baseline. Split by driver and compare: a real
role jump vs a flat role. If flat-role overs hit ~50% (regress) and role-jump overs beat break-even,
gating overs on a positive driver is the fix.

    python conviction_backtest.py [min_impact_mpg=18]
"""
import statistics as st
import sys
from collections import defaultdict

import wnba_wowy as W

IMPACT = float(sys.argv[1]) if len(sys.argv) > 1 else 18.0
MIN_NWO = 3
STATS = {"pts": "points", "reb": "rebounds", "ast": "assists"}


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

    def bracketed(n, d):
        ds = [g["date"][:10] for g in logs.get(n, [])]
        return ds and min(ds) < d and max(ds) > d

    # rows: (stat, driver_fga, d_min, over_hit)
    rows = []
    for team, names in byteam.items():
        impact = [n for n in names if pl[n]["min"] >= IMPACT]
        gids = {g["game_id"] for n in names for g in logs[n]}
        for gid in gids:
            d = gdate.get(gid)
            if not d:
                continue
            outs = [n for n in impact if bracketed(n, d) and not played(n, gid)]
            if not outs:
                continue
            out_logs = [[g for g in logs[o] if g["date"][:10] < d] for o in outs]
            for b in names:
                if b in outs or not played(b, gid):
                    continue
                actual = next(g for g in logs[b] if g["game_id"] == gid)
                if actual["min"] < 12:
                    continue
                bp = [g for g in logs[b] if g["date"][:10] < d]
                if len(bp) < 6:
                    continue
                w = W.wowy_multi(bp, out_logs)
                if w["n_without"] < MIN_NWO or w["n_with"] < 2:
                    continue
                dfga = w["without"]["fga"]["mean"] - w["with"]["fga"]["mean"]
                dmin = w["without"]["min"]["mean"] - w["with"]["min"]["mean"]
                for sk, _full in STATS.items():
                    line = round(w["with"][sk]["mean"] * 2) / 2      # stale normal-role baseline
                    rows.append((sk, dfga, dmin, 1 if actual[sk] > line else 0))

    def report(label, rs):
        n = len(rs)
        if not n:
            print(f"  {label:36} (no spots)")
            return
        hit = sum(r[3] for r in rs) / n * 100
        roi = (sum((0.91 if r[3] else -1) for r in rs) / n) * 100   # over at -110
        print(f"  {label:36} {sum(r[3] for r in rs)}-{n-sum(r[3] for r in rs)} ({hit:.0f}%)  "
              f"ROI {roi:+.0f}% @-110   n={n}")

    print(f"\nCONVICTION backtest — OVER at the stale normal-role baseline, by injury driver "
          f"(out >= {IMPACT:g}mpg), leak-free\n")
    for sk, full in STATS.items():
        sr = [r for r in rows if r[0] == sk]
        jump = [r for r in sr if (r[2] > 2 or r[1] > 1)]           # role actually expanded
        flat = [r for r in sr if not (r[2] > 2 or r[1] > 1)]       # flat/negative driver = star-over
        print(f"{full.upper()}  (break-even @-110 = 52.4%)")
        report("REAL role jump (min>2 or usage>1)", jump)
        report("FLAT role (star-over)", flat)
        print()


if __name__ == "__main__":
    main()
