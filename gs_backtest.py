"""Backtest the GAME-SCRIPT layer: does conditioning the minutes-honest projection on the
predicted game margin (blowout -> minutes haircut, close game -> bump) beat the current model
out-of-sample, on BETTING metrics?

Leak-free: the blowout predictor is each team's NET RATING from games strictly BEFORE the game
(ESPN retains no historical spread; live we'd use the real posted spread, which is strictly
sharper than this proxy — so a positive result here is a floor). Scored the way betting pays:
directional hit rate vs a blind baseline, per side, plus a predicted-blowout split.

    python gs_backtest.py [days]
"""
import statistics as st
import sys
from collections import defaultdict

import wnba_backtest_layers as B          # reuse game_ids + the cached PBP fetch

STATS = ("reb", "pts", "ast")
ROLE_FLOOR = 22
GS_SLOPE = 0.12                            # minutes per point of margin (from the mechanism probe)
MED_MARGIN = 9.0                           # median game margin (probe)
MIN_NET_GAMES = 3                          # need this many prior games for a team net rating


def richer_boxscore(gid):
    """([(pid, team, opp, {stat})...], {team: margin}) from the box score."""
    box = B.P.fetch(gid).get("boxscore", {})
    teams, tpts, order = {}, {}, []
    for tm in box.get("players", []):
        team = (tm.get("team") or {}).get("abbreviation")
        if team not in teams:
            teams[team], order = {}, order + [team]
        for stt in tm.get("statistics", []):
            keys = stt.get("keys") or []
            idx = {k: keys.index(k) for k in ("minutes", "points", "rebounds", "assists")
                   if k in keys}
            for a in stt.get("athletes", []):
                pid = a.get("athlete", {}).get("id")
                stats = a.get("stats") or []
                if not pid or not stats:
                    continue

                def num(k):
                    try:
                        return float(stats[idx[k]]) if k in idx else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                if num("minutes") > 0:
                    teams[team][pid] = {"min": num("minutes"), "pts": num("points"),
                                        "reb": num("rebounds"), "ast": num("assists")}
                    tpts[team] = tpts.get(team, 0) + num("points")
    if len(order) != 2:
        return [], {}
    a, b = order
    margin = {a: tpts.get(a, 0) - tpts.get(b, 0), b: tpts.get(b, 0) - tpts.get(a, 0)}
    rows = [(pid, team, opp, r) for team, opp in ((a, b), (b, a)) for pid, r in teams[team].items()]
    return rows, margin


def build():
    hist = defaultdict(list)               # pid -> [game dicts]
    tmargin = defaultdict(list)            # team -> [(date, margin)]
    for gid, date in B.game_ids((int(sys.argv[1]) if len(sys.argv) > 1 else 14) + 18):
        try:
            rows, margin = richer_boxscore(gid)
        except Exception:
            continue
        if not rows:
            continue
        for team, m in margin.items():
            tmargin[team].append((date, m))
        for pid, team, opp, r in rows:
            r.update({"date": date, "team": team, "opp": opp, "amargin": abs(margin.get(team, 0))})
            hist[pid].append(r)
    for pid in hist:
        hist[pid].sort(key=lambda g: g["date"])
    for t in tmargin:
        tmargin[t].sort()
    return hist, tmargin


def net(tmargin, team, date):
    ms = [m for d, m in tmargin.get(team, []) if d < date]
    return st.mean(ms) if len(ms) >= MIN_NET_GAMES else None


def elev_proj(prior, proj_min, stat):
    floor = max(proj_min - 4, ROLE_FLOOR)
    elev = [g for g in prior if g["min"] >= floor]
    if len(elev) < 4:
        return None
    return st.mean(g[stat] * min(proj_min / max(g["min"], 1), 1.35) for g in elev)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    hist, tmargin = build()
    cutoff = (B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
              - B.dt.timedelta(days=days)).strftime("%Y-%m-%d")
    METHODS = ("MH", "GS", "ORACLE")       # ORACLE adjusts on the ACTUAL margin = the ceiling if
    base = {s: {"n": 0, "over": 0} for s in STATS}          # we predicted blowouts perfectly (leak)
    ov = {m: {s: {"n": 0, "w": 0} for s in STATS} for m in METHODS}
    un = {m: {s: {"n": 0, "w": 0} for s in STATS} for m in METHODS}
    err = {m: [] for m in METHODS}
    blow_un = {m: {"n": 0, "w": 0} for m in METHODS}        # unders in PREDICTED blowouts
    n_adj = []                                              # the minutes adjustments applied
    for pid, games in hist.items():
        for i, g in enumerate(games):
            if g["date"] < cutoff:
                continue
            prior = games[:i]
            if len(prior) < 5:
                continue
            proj_min = st.mean(x["min"] for x in prior[-5:])
            na, nb = net(tmargin, g["team"], g["date"]), net(tmargin, g["opp"], g["date"])
            pred_margin = abs(na - nb) if (na is not None and nb is not None) else MED_MARGIN
            def pmin(margin_val):
                adj = max(-3.0, min(2.0, -GS_SLOPE * (margin_val - MED_MARGIN)))
                return max(proj_min + adj, 1.0), adj
            pm_gs, adj = pmin(pred_margin)
            pm_or, _ = pmin(g["amargin"])
            n_adj.append(adj)
            blowout = pred_margin >= 12
            for stat in STATS:
                projs = {"MH": elev_proj(prior, proj_min, stat),
                         "GS": elev_proj(prior, pm_gs, stat),
                         "ORACLE": elev_proj(prior, pm_or, stat)}
                if any(v is None for v in projs.values()):
                    continue
                a = g[stat]
                savg = st.mean(x[stat] for x in prior)
                L = B.math.floor(savg) + 0.5
                base[stat]["n"] += 1
                base[stat]["over"] += 1 if a > L else 0
                for m in METHODS:
                    err[m].append(abs(projs[m] - a))
                for m, pr in projs.items():
                    if pr >= L + 0.5:
                        ov[m][stat]["n"] += 1
                        ov[m][stat]["w"] += 1 if a > L else 0
                    elif pr <= L - 0.5:
                        un[m][stat]["n"] += 1
                        un[m][stat]["w"] += 1 if a < L else 0
                        if blowout:
                            blow_un[m]["n"] += 1
                            blow_un[m]["w"] += 1 if a < L else 0

    def agg(d):
        n = sum(d[s]["n"] for s in STATS)
        w = sum(d[s]["w"] for s in STATS)
        return n, (100 * w / n if n else float("nan"))

    bn = sum(base[s]["n"] for s in STATS)
    bov = sum(base[s]["over"] for s in STATS)
    base_over = 100 * bov / bn if bn else 0
    base_under = 100 - base_over
    print(f"\nGAME-SCRIPT BACKTEST — last {days} days, {bn} projections "
          f"(blind base: over {base_over:.1f}% / under {base_under:.1f}%)")
    print(f"minutes adjustment applied: mean {st.mean(n_adj):+.2f} min, "
          f"range [{min(n_adj):+.1f}, {max(n_adj):+.1f}]\n")
    print(f"  {'method':10}{'MAE':>7}{'over hit (vs base)':>22}{'under hit (vs base)':>22}")
    for m in METHODS:
        on, oh = agg(ov[m])
        un_n, uh = agg(un[m])
        mae = st.mean(err[m]) if err[m] else 0
        print(f"  {m:10}{mae:>7.2f}{oh:>12.1f}% ({oh-base_over:+.1f}) n{on:<5}"
              f"{uh:>10.1f}% ({uh-base_under:+.1f}) n{un_n}")
    print(f"\n  UNDERS in PREDICTED BLOWOUTS (|pred margin|>=12) — the layer's sweet spot:")
    for m in METHODS:
        d = blow_un[m]
        r = 100 * d["w"] / d["n"] if d["n"] else float("nan")
        print(f"    {m:8} {d['w']}-{d['n']-d['w']}  {r:.1f}%  (vs {base_under:.1f}% base)  n{d['n']}")
    print("\n  ORACLE = perfect blowout foresight (leak). If it barely beats MH, no predictor "
          "helps; if it's much better, a sharper signal (the live spread) is worth wiring.")


if __name__ == "__main__":
    main()
