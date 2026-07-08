"""Strikeout model backtest + DISPERSION study — the make-or-break analysis.

Two questions decide whether an alt-strikeout ladder can be profitable:
  1. MEAN calibration: does the model's projected mean Ks track reality (no bias)?
  2. DISPERSION (the real thesis): books price alt rungs off a FIXED count spread.
     If a pitcher's TRUE K distribution is wider/narrower than that fixed assumption,
     the alt tails are mispriced. We measure the actual spread of (K - projected_mean)
     and whether it varies by pitcher / mean level — that variation IS the edge.

Walk-forward: each start is projected from that pitcher's PRIOR starts only (no leak),
plus the opponent team's season K%. Abundant free data (statsapi gamelogs), so this
scores on thousands of real starts, independent of any betting history.

    python k_backtest.py --seasons 2025,2026 --pitchers 120
"""
from __future__ import annotations

import argparse
import statistics as st
from collections import defaultdict

import numpy as np
from scipy.stats import nbinom, poisson

from mlb import data


def league_and_teamk(season):
    tk, lg = data.team_kpct(season)
    return tk, lg


def walk_forward_starts(pid, season, tk, lg_k):
    """Yield (proj_mean, actual_k, bf, mean_level) for each start, projected from
    that pitcher's earlier starts this season + opponent team K%."""
    logs = data.pitcher_gamelog(pid, season)
    k_sum = bf_sum = n = 0
    out = []
    for g in logs:
        bf, k = g["bf"], g["k"]
        if bf < 5:
            continue
        if n >= 3:                                   # need a few priors to project
            k_pct = (k_sum + 120 * 0.22) / (bf_sum + 120)     # regressed to lg ~22%
            exp_bf = float(np.clip(bf_sum / n, 18, 27))
            opp = tk.get(g["opp_id"], lg_k)
            # log5 combine pitcher x opponent vs league
            a = k_pct * opp / lg_k
            b = (1 - k_pct) * (1 - opp) / (1 - lg_k)
            rate = a / (a + b)
            proj = rate * exp_bf
            out.append((proj, k, bf))
        k_sum += k; bf_sum += bf; n += 1
    return out


def nb_prob_over(mean, line, size):
    n = size; p = n / (n + mean)
    return float(1 - nbinom.cdf(np.floor(line), n, p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2025,2026")
    ap.add_argument("--pitchers", type=int, default=120)
    args = ap.parse_args()
    rows = []                                        # (proj, actual, bf)
    for season in [int(s) for s in args.seasons.split(",")]:
        tk, lg_k = league_and_teamk(season)
        ids = data.starter_ids(season, limit=args.pitchers)
        for i, pid in enumerate(ids):
            try:
                rows += walk_forward_starts(pid, season, tk, lg_k)
            except Exception:
                continue
            if i % 30 == 0:
                print(f"  {season}: {i}/{len(ids)} pitchers, {len(rows)} starts", flush=True)
    proj = np.array([r[0] for r in rows]); act = np.array([r[1] for r in rows])
    resid = act - proj
    print(f"\n=== {len(rows)} projected starts ===")
    print(f"MEAN calibration: proj {proj.mean():.2f} vs actual {act.mean():.2f} "
          f"(bias {resid.mean():+.2f})  |  RMSE {np.sqrt((resid**2).mean()):.2f}  "
          f"|  corr {np.corrcoef(proj, act)[0,1]:.3f}")

    # DISPERSION: actual variance vs Poisson (var=mean) vs the model's NB size=8
    print("\n=== DISPERSION by projected-mean bucket (the alt-line thesis) ===")
    print(f"{'proj K bucket':>14}{'n':>6}{'actual mean':>12}{'actual var':>11}"
          f"{'Poisson var':>12}{'var/mean':>9}")
    buckets = defaultdict(list)
    for p, a in zip(proj, act):
        buckets[int(round(p))].append(a)
    for m in sorted(buckets):
        v = buckets[m]
        if len(v) < 40:
            continue
        mean = st.mean(v); var = st.pvariance(v)
        print(f"{m:>14}{len(v):>6}{mean:>12.2f}{var:>11.2f}{mean:>12.2f}{var/mean:>9.2f}")
    allvar = st.pvariance(list(act)); allmean = st.mean(list(act))
    print(f"\noverall var/mean (dispersion index) = {allvar/allmean:.2f}  "
          f"(1.0=Poisson; >1 overdispersed => books using Poisson-ish alts underprice tails)")

    # implied best-fit NB size (dispersion) vs the model's hardcoded 8.0
    # var = mean + mean^2/size  ->  size = mean^2 / (var - mean)
    if allvar > allmean:
        implied = allmean**2 / (allvar - allmean)
        print(f"best-fit NB size = {implied:.1f}  (model uses 8.0; lower = fatter tails)")


if __name__ == "__main__":
    main()
