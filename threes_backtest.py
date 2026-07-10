"""Does DvP help the THREES market? Threes are shooting-variance dominated (not minutes), so
opponent 3PT defense has room to matter where DvP barely nudged pts/reb/ast. Leak-free: fg3m
DvP fit on pre-window games (wnba_dvp ridge); minutes-honest projection from prior games; only
real 3pt shooters (prior fg3m avg >= 1.0, the propable population).

    python threes_backtest.py [days]
"""
import statistics as st
import sys
from collections import defaultdict

import gs_backtest as G
import wnba_backtest_layers as B
import wnba_dvp as D


def build(fetch_days):
    hist, allg = defaultdict(list), []
    for gid, date in B.game_ids(fetch_days):
        try:
            rows = D._boxscore(gid)
        except Exception:
            continue
        for pid, team, opp, poss, r in rows:
            g = {**r, "pid": pid, "date": date, "team": team, "opp": opp, "poss": poss}
            hist[pid].append(g)
            allg.append(g)
    for pid in hist:
        hist[pid].sort(key=lambda g: g["date"])
    return hist, allg


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    id2pos, wnba = D.positions()
    hist, allg = build(days + 24)
    lg = st.mean(g["poss"] for g in allg if g["team"] in wnba) or 80.0
    cutoff = (B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
              - B.dt.timedelta(days=days)).strftime("%Y-%m-%d")
    train = [g for g in allg if g["date"] < cutoff and g["team"] in wnba and g["opp"] in wnba]

    dvp = {}
    for P in ("G", "F", "C"):
        rows = [(g["pid"], g["opp"], (g["fg3m"] / g["min"]) * (lg / max(g["poss"], 1)))
                for g in train if id2pos.get(g["pid"]) == P and g["min"] >= 8]
        dvp[P] = D._fit(rows)

    base = {"n": 0, "over": 0}
    ov = {m: {"n": 0, "w": 0} for m in ("MH", "DvP")}
    un = {m: {"n": 0, "w": 0} for m in ("MH", "DvP")}
    err = {m: [] for m in ("MH", "DvP")}
    fbets = []
    for pid, games in hist.items():
        P = id2pos.get(pid)
        for i, g in enumerate(games):
            if g["date"] < cutoff or g["team"] not in wnba:
                continue
            prior = games[:i]
            if len(prior) < 5 or st.mean(x["fg3m"] for x in prior) < 1.0:
                continue                                   # only real 3pt shooters
            proj_min = st.mean(x["min"] for x in prior[-5:])
            mh = G.elev_proj(prior, proj_min, "fg3m")
            if mh is None:
                continue
            d = dvp.get(P, {}).get(g["opp"], 0.0)
            dp = mh + d * proj_min
            a = g["fg3m"]
            L = B.math.floor(st.mean(x["fg3m"] for x in prior)) + 0.5
            base["n"] += 1
            base["over"] += 1 if a > L else 0
            err["MH"].append(abs(mh - a))
            err["DvP"].append(abs(dp - a))
            for m, pr in (("MH", mh), ("DvP", dp)):
                if pr >= L + 0.5:
                    ov[m]["n"] += 1
                    ov[m]["w"] += 1 if a > L else 0
                elif pr <= L - 0.5:
                    un[m]["n"] += 1
                    un[m]["w"] += 1 if a < L else 0
            if mh >= L + 0.5:
                fbets.append(("over", a > L, d))
            elif mh <= L - 0.5:
                fbets.append(("under", a < L, d))

    bo = 100 * base["over"] / base["n"] if base["n"] else 0
    bu = 100 - bo
    print(f"\nTHREES + DvP BACKTEST — last {days} days, {base['n']} shooter-games "
          f"(blind base over {bo:.1f}% / under {bu:.1f}%)\n")
    print(f"  {'method':7}{'MAE':>7}{'over hit (vs base)':>22}{'under hit (vs base)':>22}")
    for m in ("MH", "DvP"):
        orat = 100 * ov[m]["w"] / ov[m]["n"] if ov[m]["n"] else float("nan")
        urat = 100 * un[m]["w"] / un[m]["n"] if un[m]["n"] else float("nan")
        mae = st.mean(err[m]) if err[m] else 0
        print(f"  {m:7}{mae:>7.2f}{orat:>12.1f}% ({orat-bo:+.1f}) n{ov[m]['n']:<5}"
              f"{urat:>10.1f}% ({urat-bu:+.1f}) n{un[m]['n']}")

    ds = sorted(abs(b[2]) for b in fbets if b[2])
    thr = ds[2 * len(ds) // 3] if ds else 0.01
    print(f"\n  FILTER — MH over-threes by opponent 3pt-D (|coef|>{thr:.3f} = strong matchup):")
    for lbl, cond in (("soft 3pt-D (aligned)", lambda c: c > thr),
                      ("tough 3pt-D", lambda c: c < -thr)):
        grp = [b for b in fbets if b[0] == "over" and cond(b[2])]
        if grp:
            w = sum(1 for b in grp if b[1])
            print(f"     over vs {lbl:22} {w}-{len(grp)-w}  {100*w/len(grp):.1f}%  n{len(grp)}")


if __name__ == "__main__":
    main()
