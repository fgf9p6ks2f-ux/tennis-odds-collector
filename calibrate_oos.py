#!/usr/bin/env python3
"""Calibrate WNBA P(over) from the projection log and validate OUT-OF-SAMPLE.
Method: per-stat de-bias multiplier s (actual/proj) + proportional sigma k (CV of the de-biased
residual), fit on projection-log rows STRICTLY BEFORE each test date (expanding window). Compose
into calibrated P(over) for each bet (composite markets = sum of components, with an assumed intra-
player component correlation for the variance). Compare to the model's current proj_hit."""
import sqlite3, math
from statistics import mean, pstdev

PLOG = "wnba_proj_log.sqlite"; LED = "wnba_ledger.sqlite"
STATS = ["pts", "reb", "ast"]
COMP = {"points": ["pts"], "rebounds": ["reb"], "assists": ["ast"],
        "pts_reb": ["pts", "reb"], "pts_ast": ["pts", "ast"], "reb_ast": ["reb", "ast"],
        "pra": ["pts", "reb", "ast"]}
RHO = 0.35    # assumed correlation between a player's own stats (for composite variance)


def Phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def clip(p):
    return min(0.99, max(0.01, p))


pc = sqlite3.connect(PLOG); pc.row_factory = sqlite3.Row
plog = [dict(r) for r in pc.execute("SELECT * FROM projections WHERE graded=1")]
projmap = {(r["date"], r["player"]): r for r in plog}


def fit(before_date):
    """Per-stat (s, k) from proj-log rows strictly before before_date."""
    out = {}
    for st in STATS:
        ps = [(r["proj_" + st], r["actual_" + st]) for r in plog
              if r["date"] < before_date and r.get("proj_" + st) and r.get("actual_" + st) is not None
              and r["proj_" + st] > 0]
        if len(ps) < 8:
            return None
        ratios = [a / p for p, a in ps]
        s = mean(ratios)
        k = pstdev([a / (s * p) - 1 for p, a in ps]) if s > 0 else 0.3
        out[st] = (s, max(k, 0.05), len(ps))
    return out


lc = sqlite3.connect(LED); lc.row_factory = sqlite3.Row
bets = [dict(r) for r in lc.execute(
    "SELECT * FROM predictions WHERE graded=1 AND side='over' AND actual IS NOT NULL AND line IS NOT NULL")]

oos = []; skipped = 0
for b in bets:
    d = b["pred_date"]
    cal = fit(d)
    if cal is None:
        skipped += 1; continue
    comps = COMP.get(b["stat"])
    pr = projmap.get((d, b["player"]))
    if not comps or not pr:
        skipped += 1; continue
    mus, sigs, ok = [], [], True
    for cst in comps:
        s, k, _ = cal[cst]
        pj = pr.get("proj_" + cst)
        if pj is None:
            ok = False; break
        mu = s * pj
        mus.append(mu); sigs.append(k * mu)
    if not ok:
        skipped += 1; continue
    mu_sum = sum(mus)
    var = sum(x * x for x in sigs) + 2 * RHO * sum(
        sigs[i] * sigs[j] for i in range(len(sigs)) for j in range(i + 1, len(sigs)))
    sig = math.sqrt(var) if var > 0 else 1.0
    P_cal = Phi((mu_sum - b["line"]) / sig)
    oos.append((P_cal, b.get("proj_hit"), 1 if b["result"] == "over" else 0, d, b["player"], b["stat"]))


def metrics(idx):
    ps = [(o[idx], o[2]) for o in oos if o[idx] is not None]
    br = mean([(p - o) ** 2 for p, o in ps])
    ll = -mean([o * math.log(clip(p)) + (1 - o) * math.log(1 - clip(p)) for p, o in ps])
    return br, ll, len(ps)


base = mean([o[2] for o in oos])
print(f"OOS bets scored: {len(oos)}   skipped (no train / no proj match): {skipped}")
print(f"base rate (over): {base*100:.0f}%")
print()
bc = metrics(0); print(f"CALIBRATED :  Brier {bc[0]:.3f}   LogLoss {bc[1]:.3f}   (n={bc[2]})")
cc = metrics(1); print(f"CURRENT    :  Brier {cc[0]:.3f}   LogLoss {cc[1]:.3f}   (n={cc[2]})")
# a dumb baseline: always predict the base rate
bb = mean([(base - o[2]) ** 2 for o in oos]); print(f"BASE-RATE  :  Brier {bb:.3f}   (predict {base:.2f} every time)")
print()
print("reliability (calibrated) — does predicted P match actual hit rate?")
for lo, hi in [(0, 0.55), (0.55, 0.70), (0.70, 0.85), (0.85, 1.01)]:
    g = [(p, o) for p, _, o, *_ in oos if lo <= p < hi]
    if g:
        print(f"  P {lo:.2f}-{hi:.2f}:  predicted {mean([p for p,_ in g])*100:>4.0f}%   actual {mean([o for _,o in g])*100:>4.0f}% over   (n={len(g)})")
print()
full = fit("2099-01-01")
if full:
    print("fitted params on ALL data (the calibration you'd ship):")
    for st in STATS:
        s, k, n = full[st]
        print(f"  {st}:  de-bias x{s:.3f}  (model over-projects {(1-s)*100:+.0f}%)   sigma=CV {k:.2f}   (n={n})")
