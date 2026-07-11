"""INJURY-REGIME-CONDITIONAL projection — does matching tonight's EXACT out-set to a player's
closest historical comps beat a plain recency projection?

The insight (Allemand): "without Rice" is two different players depending on whether Sykes is
ALSO out (3.4 ast vs 8.6 ast). So we can't condition on one injury at a time — we condition on
the COMBINATION. Each game is a point in "who-was-out" space; we weight a player's prior games by
how closely their out-set matches tonight's, weighted by each teammate's role size (a lead guard
out matters more than a bench big), and blend that with recency.

Leak-free: only games strictly before the target. Scored on betting metrics vs a blind baseline
AND on MAE, reported overall + on the subset where the regime actually differs (a real injury
tonight) — which is the only place it can help.

    python injury_regime_backtest.py [test_days]
"""
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import gs_backtest as G
import wnba_backtest_layers as B

STATS = ("pts", "reb", "ast")
ROT_GAMES = 6            # a rotation player: >=6 games of >=12 min in the window
ROT_MIN = 12
ACTIVE_WIN = 24         # a teammate "counts" as out only if active within the trailing N days
HALFLIFE = 5.0          # recency half-life (games)
LAM = 3.5               # roster-state similarity sharpness (higher = more selective on matches)
CACHE = Path(__file__).resolve().parent / "wnba_box_cache.json"


def load(days):
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    dirty = False
    team_dates = defaultdict(dict)      # team -> date -> {pid: min}
    phist = defaultdict(list)           # pid  -> [{date,team,opp,min,pts,reb,ast}]
    for gid, date in B.game_ids(days + 34):
        if gid in cache:
            rows = cache[gid]
        else:
            try:
                r, _ = G.richer_boxscore(gid)
                rows = [[pid, team, opp, {k: d.get(k, 0) for k in ("min", *STATS)}]
                        for pid, team, opp, d in r]
            except Exception:
                rows = []
            cache[gid] = rows
            dirty = True
        for pid, team, opp, d in rows:
            if d.get("min", 0) <= 0:
                continue
            team_dates[team].setdefault(date, {})[pid] = d["min"]
            phist[pid].append({"date": date, "team": team, "opp": opp, **d})
    if dirty:
        CACHE.write_text(json.dumps(cache))
    for pid in phist:
        phist[pid].sort(key=lambda g: g["date"])
    return team_dates, phist


def _dadd(date, delta):
    return (B.dt.date.fromisoformat(date) + B.dt.timedelta(days=delta)).isoformat()


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    team_dates, phist = load(days)

    # rotation + role-size weight (mean minutes) per team
    rot = defaultdict(dict)
    for team, dd in team_dates.items():
        mins = defaultdict(list)
        for date, players in dd.items():
            for pid, mn in players.items():
                mins[pid].append(mn)
        for pid, ms in mins.items():
            if sum(1 for m in ms if m >= ROT_MIN) >= ROT_GAMES:
                rot[team][pid] = st.mean(ms)
    # each team's sorted play-dates, for the "active within trailing window" test
    tdates = {t: sorted(dd) for t, dd in team_dates.items()}

    def active(team, pid, date):
        lo = _dadd(date, -ACTIVE_WIN)
        return any(lo <= d < date and pid in team_dates[team][d] for d in tdates[team])

    def out_set(team, date, exclude):
        """rotation teammates who were ACTIVE recently but did NOT play this game (= out/rest)."""
        played = team_dates[team].get(date, {})
        return frozenset(p for p in rot[team]
                         if p != exclude and p not in played and active(team, p, date))

    cutoff = _dadd(sorted({d for dd in team_dates.values() for d in dd})[-1], -days)

    base = {"n": 0, "over": 0}
    res = {m: {"ov": [0, 0], "un": [0, 0], "ae": []} for m in ("recency", "regime")}
    sub = {m: {"ov": [0, 0], "un": [0, 0]} for m in ("recency", "regime")}   # injury-tonight subset
    dvg = {m: {"ov": [0, 0], "un": [0, 0], "ae": []} for m in ("recency", "regime")}  # regime != recent
    # divergent AND a consistent-minutes player (>=12 min most nights) -> cleaner comps
    dvc = {m: {"ov": [0, 0], "un": [0, 0], "ae": []} for m in ("recency", "regime")}
    exact_cov = [0, 0]                                                       # >=3 exact-match comps
    dvg_n = [0, 0]
    for team in rot:
        usage = rot[team]
        tot_u = sum(usage.values()) or 1
        for date in tdates[team]:
            if date < cutoff:
                continue
            out_t = out_set(team, date, None)
            for pid in list(team_dates[team][date]):
                if pid not in rot[team]:
                    continue
                prior = [g for g in phist[pid] if g["date"] < date]
                if len(prior) < 8:
                    continue
                g_now = next(g for g in phist[pid] if g["date"] == date and g["team"] == team)
                out_here = frozenset(out_t - {pid})
                pm = st.mean(x["min"] for x in prior[-5:])
                # precompute each prior game's out-set + recency + roster similarity
                n = len(prior)
                feats = []
                for i, g in enumerate(prior):
                    og = out_set(team, g["date"], pid)
                    diff = sum(usage[k] / tot_u for k in usage
                               if k != pid and ((k in out_here) != (k in og)))
                    rec = 0.5 ** ((n - 1 - i) / HALFLIFE)
                    feats.append((g, rec, math.exp(-LAM * diff), og))
                injury_tonight = len(out_here) > 0
                exact = sum(1 for _, _, _, og in feats if og == out_here)
                # REGIME-DIVERGENT: tonight's out-set poorly matches the player's RECENT games, so
                # recency is drawing from the wrong regime — the only place the layer can add value.
                recent_sim = st.mean(f[2] for f in feats[-3:])
                divergent = injury_tonight and recent_sim < 0.6
                # consistent-minutes rotation player: plays 12+ min most nights (clean comps)
                prmins = [x["min"] for x in prior]
                consistent = (sum(1 for m in prmins if m >= 12) / len(prmins) >= 0.7
                              and st.median(prmins) >= 16)
                dvg_consistent = divergent and consistent
                if injury_tonight:
                    exact_cov[0] += 1
                    exact_cov[1] += 1 if exact >= 3 else 0
                if divergent:
                    dvg_n[0] += 1
                    dvg_n[1] += 1 if consistent else 0
                for stat in STATS:
                    a = g_now[stat]
                    line = math.floor(st.mean(x[stat] for x in prior)) + 0.5
                    base["n"] += 1
                    base["over"] += 1 if a > line else 0
                    for m in ("recency", "regime"):
                        num = den = 0.0
                        for g, rec, sim, _ in feats:
                            w = rec * (sim if m == "regime" else 1.0)
                            num += w * g[stat] * min(pm / max(g["min"], 1), 1.35)
                            den += w
                        pr = num / den if den else 0
                        res[m]["ae"].append(abs(pr - a))
                        if divergent:
                            dvg[m]["ae"].append(abs(pr - a))
                        if dvg_consistent:
                            dvc[m]["ae"].append(abs(pr - a))
                        for bucket, ok in ((dvg, divergent), (dvc, dvg_consistent)):
                            if not ok:
                                continue
                            if pr >= line + 0.5:
                                bucket[m]["ov"][0] += 1
                                bucket[m]["ov"][1] += 1 if a > line else 0
                            elif pr <= line - 0.5:
                                bucket[m]["un"][0] += 1
                                bucket[m]["un"][1] += 1 if a < line else 0
                        if pr >= line + 0.5:
                            res[m]["ov"][0] += 1
                            res[m]["ov"][1] += 1 if a > line else 0
                            if injury_tonight:
                                sub[m]["ov"][0] += 1
                                sub[m]["ov"][1] += 1 if a > line else 0
                        elif pr <= line - 0.5:
                            res[m]["un"][0] += 1
                            res[m]["un"][1] += 1 if a < line else 0
                            if injury_tonight:
                                sub[m]["un"][0] += 1
                                sub[m]["un"][1] += 1 if a < line else 0

    bo = 100 * base["over"] / base["n"] if base["n"] else 0
    print(f"\nINJURY-REGIME BACKTEST — last {days} days, {base['n']} spots "
          f"(blind over {bo:.1f}% / under {100-bo:.1f}%)")
    print(f"exact-comp coverage: {exact_cov[1]}/{exact_cov[0]} injury spots have >=3 prior games "
          f"with tonight's EXACT out-set\n")
    print(f"regime-DIVERGENT spots (tonight's out-set poorly matches recent games): {dvg_n[0]} "
          f"({dvg_n[1]} of them are consistent-minutes players)\n")
    for scope, dd, showmae in (("ALL spots", res, True), ("INJURY-tonight subset", sub, False),
                               ("REGIME-DIVERGENT subset", dvg, True),
                               ("REGIME-DIVERGENT + CONSISTENT-minutes (12+ most nights)", dvc, True)):
        print(scope)
        print(f"  {'method':8}{'MAE':>7}{'over hit':>16}{'under hit':>16}" if showmae
              else f"  {'method':8}{'over hit':>16}{'under hit':>16}")
        for m in ("recency", "regime"):
            o, u = dd[m]["ov"], dd[m]["un"]
            orate = 100 * o[1] / o[0] if o[0] else float("nan")
            urate = 100 * u[1] / u[0] if u[0] else float("nan")
            if showmae:
                mae = st.mean(dd[m]["ae"]) if dd[m]["ae"] else float("nan")
                print(f"  {m:8}{mae:>7.2f}{orate:>10.1f}% n{o[0]:<4}{urate:>9.1f}% n{u[0]}")
            else:
                print(f"  {m:8}{orate:>10.1f}% n{o[0]:<4}{urate:>9.1f}% n{u[0]}")
        print()


if __name__ == "__main__":
    main()
