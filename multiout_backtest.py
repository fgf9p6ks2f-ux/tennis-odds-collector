"""Does the MULTI-out signal SELECT beatable overs better than the single-out signal?

Not 'is the multi-out mean a good point estimate' (it isn't — small both-out samples over-fit).
The user's edge is the OVER on a LOW, stale line: the book anchors the line near the player's
NORMAL-role average, but with 2 impact players out the role compounds and she clears it. So the
bet-relevant test is: in multi-out spots, bet the OVER at the stale (normal-role) line — and does
conditioning on the FULL out-set pick the winners better than the single-out split the model uses?

Leak-free: for team-game G on date D, only games before D. 'out' for G = log brackets D but no row
at G. line = beneficiary's mean pts before D in games where >=1 of the outs PLAYED (the normal-role
baseline the book anchors on). selector = usage jump (without-minus-with FGA), single vs multi.

    python multiout_backtest.py [min_impact_mpg=14]
"""
import statistics as st
import sys
from collections import defaultdict

import wnba_wowy as W

IMPACT = float(sys.argv[1]) if len(sys.argv) > 1 else 14.0
MIN_NWO = 3


def rate(xs):
    return (sum(xs) / len(xs) * 100) if xs else 0.0


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

    spots = []          # (actual_over, single_jump, multi_jump, line, actual)
    for team, names in byteam.items():
        impact = [n for n in names if pl[n]["min"] >= IMPACT]
        gids = {g["game_id"] for n in names for g in logs[n]}
        for gid in gids:
            d = gdate.get(gid)
            if not d:
                continue
            outs = [n for n in impact if bracketed(n, d) and not played(n, gid)]
            if len(outs) < 2:
                continue
            top_out = max(outs, key=lambda n: pl[n]["min"])
            for b in names:
                if b in outs or not played(b, gid):
                    continue
                actual = next(g for g in logs[b] if g["game_id"] == gid)
                if actual["min"] < 10:
                    continue
                bp = [g for g in logs[b] if g["date"][:10] < d]
                if len(bp) < 6:
                    continue
                outp = {o: [g for g in logs[o] if g["date"][:10] < d] for o in outs}
                single = W.wowy(bp, outp[top_out])
                multi = W.wowy_multi(bp, list(outp.values()))
                if single["n_without"] < MIN_NWO or multi["n_without"] < MIN_NWO or multi["n_with"] < 2:
                    continue
                line = round(multi["with"]["pts"]["mean"] * 2) / 2      # stale normal-role line
                sj = single["without"]["fga"]["mean"] - single["with"]["fga"]["mean"]
                mj = multi["without"]["fga"]["mean"] - multi["with"]["fga"]["mean"]
                spots.append((1 if actual["pts"] > line else 0, sj, mj, line, actual["pts"]))

    n = len(spots)
    print(f"\nMULTI-OUT over-SELECTION backtest — {n} beneficiary-spots (2+ impact >={IMPACT:g}mpg out), "
          f"leak-free\n")
    print(f"  OVER at the stale normal-role line, ALL multi-out spots: {rate([s[0] for s in spots]):.0f}% "
          f"(break-even -110 = 52.4%)\n")
    for thr in (1.0, 2.0, 3.0):
        sel_s = [s for s in spots if s[1] >= thr]      # single-out usage jump >= thr
        sel_m = [s for s in spots if s[2] >= thr]      # multi-out  usage jump >= thr
        print(f"  usage-jump >= +{thr:g} FGA as the filter:")
        print(f"     SINGLE-out selects {len(sel_s):3} spots -> over hits {rate([s[0] for s in sel_s]):.0f}%")
        print(f"     MULTI-out  selects {len(sel_m):3} spots -> over hits {rate([s[0] for s in sel_m]):.0f}%")
    # the spots where multi sees a jump the single-out split MISSES (diluted) — the user's case
    hidden = [s for s in spots if s[2] >= 2.0 and s[1] < 1.0]
    print(f"\n  spots MULTI flags (>=+2 FGA) that SINGLE misses (<+1): {len(hidden)} "
          f"-> over hits {rate([s[0] for s in hidden]):.0f}%")


if __name__ == "__main__":
    main()
