"""Does RECENCY weighting fix the ascending-player miss (Allemand: last-3 = 10 ast, model
projects 5.7)? The current model flat-averages a player's whole elevated history and uses a
stale minutes estimate. Recency weighting (a) uses trailing-3 minutes and (b) weights recent
elevated games more (exponential half-life). Leak-free; scored on betting metrics vs a blind
baseline, per side, PLUS a split on ASCENDING players (trailing-3 min > trailing-8 min) — where
the flaw lives.

    python recency_backtest.py [days]
"""
import statistics as st
import sys
from collections import defaultdict

import gs_backtest as G
import wnba_backtest_layers as B

STATS = ("reb", "pts", "ast")
ROLE_FLOOR = 22
HALFLIFE = 4.0                    # elevated games; recent weighted 2x every 4 games back


def rw_proj(prior, proj_min, stat):
    floor = max(proj_min - 4, ROLE_FLOOR)
    elev = sorted((g for g in prior if g["min"] >= floor), key=lambda g: g["date"])
    if len(elev) < 4:
        return None
    n = len(elev)
    ws = [0.5 ** ((n - 1 - i) / HALFLIFE) for i in range(n)]     # newest games heaviest
    vals = [g[stat] * min(proj_min / max(g["min"], 1), 1.35) for g in elev]
    return sum(v * w for v, w in zip(vals, ws)) / sum(ws)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 21
    hist = defaultdict(list)
    for gid, date in B.game_ids(days + 24):
        try:
            rows, _ = G.richer_boxscore(gid)
        except Exception:
            continue
        for pid, team, opp, r in rows:
            hist[pid].append({**r, "date": date})
    for pid in hist:
        hist[pid].sort(key=lambda g: g["date"])
    cutoff = (B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
              - B.dt.timedelta(days=days)).strftime("%Y-%m-%d")

    base = {"n": 0, "over": 0}
    ov = {m: {"n": 0, "w": 0} for m in ("MH", "RW")}
    un = {m: {"n": 0, "w": 0} for m in ("MH", "RW")}
    err = {m: [] for m in ("MH", "RW")}
    asc = {m: {"o": {"n": 0, "w": 0}, "u": {"n": 0, "w": 0}} for m in ("MH", "RW")}  # ASCENDING players
    for pid, games in hist.items():
        for i, g in enumerate(games):
            if g["date"] < cutoff:
                continue
            prior = games[:i]
            if len(prior) < 8:
                continue
            pm_mh = st.mean(x["min"] for x in prior[-5:])       # current: trailing-5
            pm_rw = st.mean(x["min"] for x in prior[-3:])       # recency: trailing-3
            ascending = st.mean(x["min"] for x in prior[-3:]) > st.mean(x["min"] for x in prior[-8:]) + 2
            for stat in STATS:
                mh = G.elev_proj(prior, pm_mh, stat)
                rw = rw_proj(prior, pm_rw, stat)
                if mh is None or rw is None:
                    continue
                a = g[stat]
                L = B.math.floor(st.mean(x[stat] for x in prior)) + 0.5
                base["n"] += 1
                base["over"] += 1 if a > L else 0
                err["MH"].append(abs(mh - a))
                err["RW"].append(abs(rw - a))
                for m, pr in (("MH", mh), ("RW", rw)):
                    if pr >= L + 0.5:
                        ov[m]["n"] += 1
                        ov[m]["w"] += 1 if a > L else 0
                        if ascending:
                            asc[m]["o"]["n"] += 1
                            asc[m]["o"]["w"] += 1 if a > L else 0
                    elif pr <= L - 0.5:
                        un[m]["n"] += 1
                        un[m]["w"] += 1 if a < L else 0
                        if ascending:
                            asc[m]["u"]["n"] += 1
                            asc[m]["u"]["w"] += 1 if a < L else 0

    bo = 100 * base["over"] / base["n"] if base["n"] else 0
    bu = 100 - bo
    print(f"\nRECENCY-WEIGHTING BACKTEST — last {days} days, {base['n']} spots "
          f"(blind over {bo:.1f}% / under {bu:.1f}%)\n")
    print(f"  {'method':7}{'MAE':>7}{'over hit (vs base)':>22}{'under hit (vs base)':>22}")
    for m in ("MH", "RW"):
        orat = 100 * ov[m]["w"] / ov[m]["n"] if ov[m]["n"] else float("nan")
        urat = 100 * un[m]["w"] / un[m]["n"] if un[m]["n"] else float("nan")
        mae = st.mean(err[m]) if err[m] else 0
        print(f"  {m:7}{mae:>7.2f}{orat:>12.1f}% ({orat-bo:+.1f}) n{ov[m]['n']:<5}"
              f"{urat:>10.1f}% ({urat-bu:+.1f}) n{un[m]['n']}")
    print(f"\n  ASCENDING players (trailing-3 min > trailing-8 by 2+) — where the flaw lives:")
    for m in ("MH", "RW"):
        o, u = asc[m]["o"], asc[m]["u"]
        orat = 100 * o["w"] / o["n"] if o["n"] else float("nan")
        urat = 100 * u["w"] / u["n"] if u["n"] else float("nan")
        print(f"    {m:5} overs {o['w']}-{o['n']-o['w']} ({orat:.0f}%)  |  unders {u['w']}-{u['n']-u['w']} ({urat:.0f}%)")
    print("  (on ascending players the current model bets too many UNDERS that lose; RW should "
          "flip some to overs and/or cut the losing unders.)")


if __name__ == "__main__":
    main()
