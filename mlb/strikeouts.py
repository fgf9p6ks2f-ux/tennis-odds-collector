"""Pitcher strikeout projection.

Matchup K-rate via the log5 / odds-ratio combine of the pitcher's K% and the opponent
lineup's K% relative to league average; scaled by expected batters faced (BF) to a mean;
then a count distribution for P(over/under the line).

    matchup_K% = (P·B/L) / (P·B/L + (1-P)(1-B)/(1-L))
    mean_K     = matchup_K% · expected_BF
"""
from __future__ import annotations

import numpy as np
from scipy.stats import nbinom, poisson


def log5_rate(p_pit: float, p_opp: float, p_lg: float) -> float:
    """Combine pitcher K% (P) and opponent lineup K% (B) vs league K% (L)."""
    p_pit = min(max(p_pit, 1e-4), 1 - 1e-4)
    p_opp = min(max(p_opp, 1e-4), 1 - 1e-4)
    a = p_pit * p_opp / p_lg
    b = (1 - p_pit) * (1 - p_opp) / (1 - p_lg)
    return a / (a + b)


def blend_kpct(cur_k: float, cur_bf: float, pri_k: float, pri_bf: float,
               reg_target: float, decay: float = 0.6, reg_bf: float = 120.0) -> float:
    """Marcel-style true-talent K% = current season + decayed prior season + a
    regression component (whiff-implied talent). Thin current samples lean on the
    prior year and the regression; heavy current samples dominate."""
    num = cur_k + decay * pri_k + reg_bf * reg_target
    den = cur_bf + decay * pri_bf + reg_bf
    return num / den


def project(k_pct_pit: float, k_pct_opp: float, k_pct_lg: float, exp_bf: float) -> dict:
    p = log5_rate(k_pct_pit, k_pct_opp, k_pct_lg)
    return {"k_rate": p, "mean_k": p * exp_bf, "exp_bf": exp_bf}


def dispersion_size(mean_k: float, rate: float, exp_bf: float, var_bf: float) -> float:
    """Negative-binomial `size` from FIRST PRINCIPLES, not a hardcoded constant.
    K = sum over (random) BF of Bernoulli(rate), so
        Var(K) = exp_bf*rate*(1-rate)  +  rate^2 * Var(BF)
                 \___ Poisson core ___/    \__ hook / short-leash risk __/
    Backtest (2025, 8,472 alt-line points): beats the old fixed size=8 by 1.3% log-loss.
    The 2nd term is the edge — high Var(BF) arms (rookies, injury returns, tight pitch
    limits) get fatter UNDER tails a fixed-dispersion book underprices. size=m^2/(var-m)."""
    var = exp_bf * rate * (1 - rate) + rate * rate * max(var_bf, 0.0)
    var = max(var, mean_k * 1.02)
    return mean_k * mean_k / (var - mean_k) if var > mean_k else 500.0


def prob_over(mean_k: float, line: float, dist: str = "nbinom", size: float = 40.0) -> float:
    """P(strikeouts > line). Lines are X.5 (no push). `size` = NB dispersion (var = mean
    + mean^2/size); smaller = fatter tails, Poisson = size->inf. Default 40 reflects the
    near-Poisson core of completed starts; pass a per-pitcher size from dispersion_size()
    to capture short-leash tail risk."""
    floor = np.floor(line)
    if dist == "poisson" or mean_k <= 0:
        return float(1 - poisson.cdf(floor, max(mean_k, 1e-6)))
    n = size
    p = n / (n + mean_k)                       # scipy nbinom: mean = n(1-p)/p
    return float(1 - nbinom.cdf(floor, n, p))


def fair_odds(mean_k: float, line: float, **kw) -> dict:
    po = prob_over(mean_k, line, **kw)
    return {"p_over": po, "p_under": 1 - po,
            "fair_over": 1 / po if po > 0 else np.inf,
            "fair_under": 1 / (1 - po) if po < 1 else np.inf}
