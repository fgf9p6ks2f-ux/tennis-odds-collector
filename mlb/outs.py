"""Pitcher OUTS-recorded projection ("Pitching Outs" prop).

Outs = a durability stat (how deep the start goes), driven by the pitcher's own
outs/start tendency + manager hook — far more STABLE than strikeouts, so it projects
tighter (lower MAE) and is a better prop to model. Opponent matters only weakly
(a tougher lineup raises the pitch count → slightly earlier hook).

    mean_outs = shrink(recent outs/start toward league) - small opponent penalty
    P(over line) via Normal(mean, sd) — sd fit empirically (~3-4 outs).
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

LG_OUTS = 17.5           # league-ish outs/start prior (~5.2 IP)
PRIOR_STARTS = 4.0       # shrinkage weight (starts)


def project(outs_sum: float, n_starts: int, opp_obp: float = None,
            lg_obp: float = 0.315, opp_weight: float = 12.0) -> float:
    """Mean projected outs. Shrinks the pitcher's outs/start toward the league prior;
    subtracts a small penalty when the opponent gets on base more than average."""
    mean = (outs_sum + PRIOR_STARTS * LG_OUTS) / (n_starts + PRIOR_STARTS)
    if opp_obp is not None:
        mean -= opp_weight * (opp_obp - lg_obp)   # tougher lineup -> earlier hook
    return mean


def prob_over(mean_outs: float, line: float, sd: float = 3.6) -> float:
    return float(1 - norm.cdf((line - mean_outs) / sd))


def fair_odds(mean_outs: float, line: float, sd: float = 3.6) -> dict:
    po = prob_over(mean_outs, line, sd)
    return {"p_over": po, "p_under": 1 - po,
            "fair_over": 1 / po if po > 0 else np.inf,
            "fair_under": 1 / (1 - po) if po < 1 else np.inf}
