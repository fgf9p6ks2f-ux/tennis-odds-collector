"""State-of-the-art DvP (Defense vs Position) for WNBA — opponent- and pace-adjusted — plus a
leak-free backtest of whether it improves the projection.

Naive DvP ("what a team allows to position P") is badly confounded: it reflects WHO they faced
(schedule) and how FAST they played (pace), not defensive skill. This builds opponent-adjusted
DvP via RIDGE regression (the RAPM idea). For each stat S and position group P, every position-P
player-game's PACE-ADJUSTED per-minute rate is modeled as

        rate_ig = league_mean(P) + offense_player_i + defense_opponent_j + noise

solved with L2 shrinkage so thin per-team samples regress to the league mean. The
`defense_opponent_j` coefficients ARE the opponent-adjusted DvP: team j's true tendency to allow
S to position P, net of schedule and pace. Then: does adding that term to the minutes-honest
projection beat it out-of-sample on betting metrics? DvP is fit ONLY on games before the test
window (leak-free); MH uses each player's prior games.

    python dvp_backtest.py [days]
"""
import statistics as st
import sys
from collections import defaultdict

import numpy as np

import gs_backtest as G          # elev_proj
import wnba_backtest_layers as B
import wnba_wowy as W

STATS = ("reb", "pts", "ast")
_PG = {"G": "G", "PG": "G", "SG": "G", "GF": "G", "F": "F", "SF": "F", "PF": "F",
       "FC": "C", "C": "C", "CF": "C"}
LAMBDA = 60.0                    # ridge penalty (shrinkage strength)
MIN_MIN = 8.0                    # only rows with >=8 min feed the DvP fit


def positions():
    """{pid: G/F/C} from rosters only (fast — no game-log fetch)."""
    out = {}
    teams = W._get(f"{W.SITE}/teams").get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    for t in teams:
        for a in W._get(f"{W.SITE}/teams/{t['team']['id']}/roster").get("athletes", []):
            pid = a.get("id")
            pos = (a.get("position") or {}).get("abbreviation", "")
            if pid:
                out[pid] = _PG.get(pos, "F")
    return out


def full_boxscore(gid):
    """[(pid, team, opp, team_possessions, {min,pts,reb,ast})] with pace fields parsed."""
    box = B.P.fetch(gid).get("boxscore", {})
    teams, poss, order = {}, {}, []
    for tm in box.get("players", []):
        team = (tm.get("team") or {}).get("abbreviation")
        if team not in teams:
            teams[team], poss[team] = {}, 0.0
            order.append(team)
        for stt in tm.get("statistics", []):
            keys = stt.get("keys") or []

            def gi(k):
                return keys.index(k) if k in keys else None

            im, ip, ir, ia = gi("minutes"), gi("points"), gi("rebounds"), gi("assists")
            ifga = gi("fieldGoalsMade-fieldGoalsAttempted")
            ifta = gi("freeThrowsMade-freeThrowsAttempted")
            itov, ioreb = gi("turnovers"), gi("offensiveRebounds")
            for a in stt.get("athletes", []):
                pid = a.get("athlete", {}).get("id")
                s = a.get("stats") or []
                if not pid or not s:
                    continue

                def num(i):
                    try:
                        return float(s[i]) if i is not None else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                def att(i):
                    try:
                        return float(s[i].split("-")[1]) if i is not None else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                mn = num(im)
                poss[team] += att(ifga) + 0.44 * att(ifta) - num(ioreb) + num(itov)
                if mn > 0:
                    teams[team][pid] = {"min": mn, "pts": num(ip), "reb": num(ir), "ast": num(ia)}
    if len(order) != 2:
        return []
    a, b = order
    return [(pid, team, opp, poss[team], r)
            for team, opp in ((a, b), (b, a)) for pid, r in teams[team].items()]


def build(fetch_days):
    hist = defaultdict(list)
    allg = []
    for gid, date in B.game_ids(fetch_days):
        try:
            rows = full_boxscore(gid)
        except Exception:
            continue
        for pid, team, opp, tposs, r in rows:
            g = {**r, "pid": pid, "date": date, "team": team, "opp": opp, "poss": tposs}
            hist[pid].append(g)
            allg.append(g)
    for pid in hist:
        hist[pid].sort(key=lambda g: g["date"])
    return hist, allg


def fit_dvp(rows, lg_pace, lam=LAMBDA):
    """rows: [(pid, team_defending, pace_adj_per_min_rate)] -> {team: opponent-adjusted DvP coef}
    via ridge on [intercept + player dummies + team dummies]. Positive = allows more."""
    pids = sorted({r[0] for r in rows})
    teams = sorted({r[1] for r in rows})
    if len(rows) < 40 or not teams:
        return {}, {}
    pi = {p: i for i, p in enumerate(pids)}
    ti = {t: i for i, t in enumerate(teams)}
    npl, nt = len(pids), len(teams)
    p = 1 + npl + nt
    X = np.zeros((len(rows), p))
    y = np.array([r[2] for r in rows])
    X[:, 0] = 1.0
    for i, (pid, team, _) in enumerate(rows):
        X[i, 1 + pi[pid]] = 1.0
        X[i, 1 + npl + ti[team]] = 1.0
    reg = np.ones(p)
    reg[0] = 0.0                                   # don't penalise the intercept
    beta = np.linalg.solve(X.T @ X + lam * np.diag(reg), X.T @ y)
    adj = {t: beta[1 + npl + ti[t]] for t in teams}
    # naive DvP (pace-adjusted, but NOT opponent-adjusted) for comparison
    lgmean = y.mean()
    byt = defaultdict(list)
    for (pid, team, yy) in rows:
        byt[team].append(yy)
    naive = {t: st.mean(v) - lgmean for t, v in byt.items()}
    return adj, naive


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    id2pos = positions()
    hist, allg = build(days + 24)
    lg_pace = st.mean(g["poss"] for g in allg) or 80.0
    cutoff = (B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
              - B.dt.timedelta(days=days)).strftime("%Y-%m-%d")
    train = [g for g in allg if g["date"] < cutoff]
    print(f"\nDvP BACKTEST — fit on {len(train)} pre-window player-games, test last {days} days "
          f"(league pace {lg_pace:.1f} poss/g, ridge λ={LAMBDA})\n")

    # fit opponent-adjusted DvP per (stat, position) on the TRAIN period
    dvp = {}          # (stat, pos) -> {team: adj_coef}
    naive_corr = {}
    for stat in STATS:
        for P in ("G", "F", "C"):
            rows = [(g["pid"], g["opp"], (g[stat] / g["min"]) * (lg_pace / max(g["poss"], 1)))
                    for g in train if id2pos.get(g["pid"]) == P and g["min"] >= MIN_MIN]
            adj, naive = fit_dvp(rows, lg_pace)
            dvp[(stat, P)] = adj
            if adj and len(adj) >= 5:
                common = [t for t in adj if t in naive]
                a = [adj[t] for t in common]
                b = [naive[t] for t in common]
                ma, mb = st.mean(a), st.mean(b)
                cov = sum((x - ma) * (yv - mb) for x, yv in zip(a, b))
                va = sum((x - ma) ** 2 for x in a)
                vb = sum((yv - mb) ** 2 for yv in b)
                naive_corr[(stat, P)] = cov / (va * vb) ** 0.5 if va and vb else float("nan")

    # show the analytical output for points-vs-guards (the headline market)
    for (stat, P) in (("pts", "G"), ("reb", "C")):
        adj = dvp.get((stat, P), {})
        if adj:
            rank = sorted(adj.items(), key=lambda kv: kv[1])
            print(f"  opponent-adjusted DvP — {stat} allowed to {P} (per-min pace-adj, +=softer D):")
            soft = ", ".join(f"{t} {v:+.3f}" for t, v in rank[-3:][::-1])
            tough = ", ".join(f"{t} {v:+.3f}" for t, v in rank[:3])
            print(f"     softest: {soft}   |   toughest: {tough}")
            c = naive_corr.get((stat, P))
            if c is not None:
                print(f"     corr(adjusted, naive DvP) = {c:+.2f}  "
                      f"({'adjustment reorders a lot' if c < 0.6 else 'similar to naive'})")
    print()

    # BACKTEST: MH vs MH+DvP on betting metrics, leak-free
    base = {"n": 0, "over": 0}
    ov = {m: {"n": 0, "w": 0} for m in ("MH", "DvP")}
    un = {m: {"n": 0, "w": 0} for m in ("MH", "DvP")}
    err = {m: [] for m in ("MH", "DvP")}
    adjmag = []
    fbets = []            # (side, won, dvp_coef) for the filter view
    for pid, games in hist.items():
        P = id2pos.get(pid)
        for i, g in enumerate(games):
            if g["date"] < cutoff:
                continue
            prior = games[:i]
            if len(prior) < 5:
                continue
            proj_min = st.mean(x["min"] for x in prior[-5:])
            for stat in STATS:
                mh = G.elev_proj(prior, proj_min, stat)
                if mh is None:
                    continue
                d = dvp.get((stat, P), {}).get(g["opp"], 0.0)
                dvp_proj = mh + d * proj_min           # add the opponent-adjusted term
                adjmag.append(abs(d * proj_min))
                a = g[stat]
                savg = st.mean(x[stat] for x in prior)
                L = B.math.floor(savg) + 0.5
                base["n"] += 1
                base["over"] += 1 if a > L else 0
                err["MH"].append(abs(mh - a))
                err["DvP"].append(abs(dvp_proj - a))
                for m, pr in (("MH", mh), ("DvP", dvp_proj)):
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
    print(f"  betting metrics (blind base over {bo:.1f}% / under {bu:.1f}%, {base['n']} spots) — "
          f"DvP adj applied: mean {st.mean(adjmag):.2f} stat-units\n")
    print(f"  {'method':7}{'MAE':>7}{'over hit (vs base)':>22}{'under hit (vs base)':>22}")
    for m in ("MH", "DvP"):
        orat = 100 * ov[m]["w"] / ov[m]["n"] if ov[m]["n"] else float("nan")
        urat = 100 * un[m]["w"] / un[m]["n"] if un[m]["n"] else float("nan")
        mae = st.mean(err[m]) if err[m] else 0
        print(f"  {m:7}{mae:>7.2f}{orat:>12.1f}% ({orat-bo:+.1f}) n{ov[m]['n']:<5}"
              f"{urat:>10.1f}% ({urat-bu:+.1f}) n{un[m]['n']}")

    # FILTER view: the model's MH bets split by the opponent's DvP (soft vs tough for that stat)
    ds = sorted(abs(b[2]) for b in fbets if b[2])
    thr = ds[2 * len(ds) // 3] if ds else 0.01           # top-third |DvP| = a real matchup edge
    print(f"\n  FILTER — MH bets split by opponent DvP (|coef|>{thr:.3f} = strong matchup):")
    for side in ("over", "under"):
        good = "soft" if side == "over" else "tough"      # overs want soft D, unders want tough D
        aligned = [b for b in fbets if b[0] == side
                   and ((b[2] > thr) if side == "over" else (b[2] < -thr))]
        against = [b for b in fbets if b[0] == side
                   and ((b[2] < -thr) if side == "over" else (b[2] > thr))]
        for lbl, grp in ((f"vs {good} D (aligned)", aligned), ("vs opposite D", against)):
            if grp:
                w = sum(1 for b in grp if b[1])
                print(f"     {side:5} {lbl:22} {w}-{len(grp)-w}  {100*w/len(grp):.1f}%  n{len(grp)}")


if __name__ == "__main__":
    main()
