"""Backtest the OPPONENT-INJURIES layer: do players beat their minutes-honest projection when
the OPPONENT is missing rotation regulars — and does knowing that improve the bets?

Leak-free: a team's regulars are defined from games BEFORE the game (avg min >= 15, >=3 games);
"missing" = a regular absent (or <5 min) from that game's box score, which was ruled out pre-game.
So `opp_missing_min` (sum of the absent regulars' prior avg minutes) is knowable pre-game.

Gate first (residual = actual - MH projection, vs opponent depletion), then the betting split:
do the model's UNDERS become traps when the opponent is depleted (player over-produces)?

    python oi_backtest.py [days]
"""
import statistics as st
import sys
from collections import defaultdict

import wnba_backtest_layers as B
import gs_backtest as G          # reuse richer_boxscore + elev_proj

STATS = ("reb", "pts", "ast")


def build(fetch_days):
    hist = defaultdict(list)
    lineup = defaultdict(dict)          # team -> {date: {pid: min}}
    team_players = defaultdict(set)     # team -> {pid}
    for gid, date in B.game_ids(fetch_days):
        try:
            rows, _ = G.richer_boxscore(gid)
        except Exception:
            continue
        if not rows:
            continue
        for pid, team, opp, r in rows:
            r = dict(r)
            r.update({"date": date, "team": team, "opp": opp})
            hist[pid].append(r)
            team_players[team].add(pid)
            lineup[team].setdefault(date, {})[pid] = r["min"]
    for pid in hist:
        hist[pid].sort(key=lambda g: g["date"])
    return hist, lineup, team_players


def avg_min_before(games, date):
    ms = [g["min"] for g in games if g["date"] < date]
    return st.mean(ms) if len(ms) >= 3 else None


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    hist, lineup, team_players = build(days + 18)
    cutoff = (B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
              - B.dt.timedelta(days=days)).strftime("%Y-%m-%d")

    def opp_missing_min(opp, date):
        present = {p for p, m in lineup[opp].get(date, {}).items() if m >= 5}
        miss = 0.0
        for pid in team_players[opp]:
            am = avg_min_before(hist[pid], date)
            if am is not None and am >= 15 and pid not in present:
                miss += am
        return miss

    resid = defaultdict(list)                 # stat -> [(opp_missing_min, residual)]
    bets = []                                  # (side, won, opp_missing_min)  at the season-avg line
    base = {"n": 0, "over": 0}
    for pid, games in hist.items():
        for i, g in enumerate(games):
            if g["date"] < cutoff:
                continue
            prior = games[:i]
            if len(prior) < 5:
                continue
            proj_min = st.mean(x["min"] for x in prior[-5:])
            mm = opp_missing_min(g["opp"], g["date"])
            for stat in STATS:
                mh = G.elev_proj(prior, proj_min, stat)
                if mh is None:
                    continue
                a = g[stat]
                resid[stat].append((mm, a - mh))
                savg = st.mean(x[stat] for x in prior)
                L = B.math.floor(savg) + 0.5
                base["n"] += 1
                base["over"] += 1 if a > L else 0
                if mh >= L + 0.5:
                    bets.append(("over", a > L, mm))
                elif mh <= L - 0.5:
                    bets.append(("under", a < L, mm))

    base_under = 100 - 100 * base["over"] / base["n"] if base["n"] else 0
    print(f"\nOPPONENT-INJURIES BACKTEST — last {days} days, {base['n']} projections "
          f"(blind under base {base_under:.1f}%)\n")

    # GATE: does opponent depletion predict OVER-production (positive residual)?
    print("  GATE — mean residual (actual - MH projection) by opponent depletion:")
    HEAL_MAX, DEP_MIN = 5.0, 25.0       # opp at ~full strength vs missing ~a rotation regular

    def mn(v):
        return st.mean(v) if v else float("nan")

    print(f"   (healthy = opp_missing_min <= {HEAL_MAX:.0f}; depleted >= {DEP_MIN:.0f} ≈ a starter out)")
    for stat in STATS:
        r = resid[stat]
        heal = [d for m, d in r if m <= HEAL_MAX]
        dep = [d for m, d in r if m >= DEP_MIN]
        ms = [m for m, _ in r]
        ds = [d for _, d in r]
        mx, mdv = st.mean(ms), st.mean(ds)
        cov = sum((m - mx) * (d - mdv) for m, d in r)
        vx = sum((m - mx) ** 2 for m in ms)
        vy = sum((d - mdv) ** 2 for d in ds)
        corr = cov / (vx * vy) ** 0.5 if vx and vy else 0
        print(f"     {stat:4} healthy-opp {mn(heal):+.2f}  depleted-opp {mn(dep):+.2f}  "
              f"(Δ {mn(dep) - mn(heal):+.2f})  corr {corr:+.3f}  n{len(r)} (dep n{len(dep)})")

    # BETTING: are the model's UNDERS traps vs depleted opponents? are OVERS better?
    print(f"\n  BETTING — model's bets split by opponent depletion (are depleted-opp unders traps?):")
    for side in ("under", "over"):
        sb = [b for b in bets if b[0] == side]
        heal = [b for b in sb if b[2] <= HEAL_MAX]
        dep = [b for b in sb if b[2] >= DEP_MIN]
        for lbl, grp in (("healthy opp", heal), ("depleted opp", dep)):
            if grp:
                w = sum(1 for b in grp if b[1])
                print(f"     {side:5} vs {lbl:13} {w}-{len(grp)-w}  {100*w/len(grp):.1f}%  n{len(grp)}")
    print("\n  Read: if UNDER hit% drops sharply vs depleted opponents, those unders are traps we "
          "should skip/flip; if OVER hit% rises, opponent injuries are a real, exploitable signal.")


if __name__ == "__main__":
    main()
