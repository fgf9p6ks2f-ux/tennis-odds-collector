"""Shared pitcher-strikeout projection (whiff-skill model — the backtest-best config)
so the live scan and the edge backtest use identical logic.
"""
from __future__ import annotations

import numpy as np

from . import strikeouts

PRIOR_BF = 120.0


def whiff_fit(whiff: dict) -> tuple[float, float]:
    """Fit K% ~ whiff% across pitchers -> (intercept a, slope b). Used to turn a
    pitcher's whiff rate into a true-talent K% shrinkage prior."""
    pairs = [(w, k) for k, w in whiff.values()]
    if len(pairs) > 20:
        b, a = np.polyfit([w for w, _ in pairs], [k for _, k in pairs], 1)
        return float(a), float(b)
    return 0.22, 0.0


def whiff_prior(pid: int, whiff: dict, a: float, b: float, lg_k: float) -> float:
    if pid in whiff:
        return float(np.clip(a + b * whiff[pid][1], 0.10, 0.40))
    return lg_k


# plate appearances by batting-order slot (leadoff bats most) — for weighting a lineup
PA_WEIGHTS = [4.65, 4.55, 4.45, 4.35, 4.25, 4.12, 4.00, 3.90, 3.80]


def lineup_kpct(batter_ids: list[int], hand: str, splits: dict, lg_k: float) -> float:
    """PA-weighted K% of the actual lineup vs the starter's hand. Falls back to league
    K% for batters without a reliable split. Returns None if the lineup is empty."""
    if not batter_ids:
        return None
    num = den = 0.0
    for i, bid in enumerate(batter_ids[:9]):
        w = PA_WEIGHTS[i] if i < len(PA_WEIGHTS) else 3.8
        kp = (splits.get(bid) or {}).get(hand, lg_k)
        num += w * kp
        den += w
    return num / den if den else lg_k


# Home/away calibration (k_characteristics study, 2,378 starts 2025-26): the model
# under-projects HOME starters. Pooled bias was +0.28 home / -0.06 away; we apply a
# CONSERVATIVE half of the stable home lift (away flipped sign in 2026, so leave it ~0)
# — a small accuracy fix, NOT a standalone edge (sharp books already price home/away).
HOME_ADJ = 0.15


def project_mean(k_sum: float, bf_sum: float, n_starts: int, opp_kpct: float,
                 lg_k: float, w_prior: float, is_home: bool | None = None) -> float:
    """Mean projected Ks for one start. `is_home` applies the home/away calibration."""
    k_pct = (k_sum + PRIOR_BF * w_prior) / (bf_sum + PRIOR_BF)
    exp_bf = float(np.clip(bf_sum / max(n_starts, 1), 18, 27))
    mean = strikeouts.project(k_pct, opp_kpct, lg_k, exp_bf)["mean_k"]
    if is_home is True:
        mean += HOME_ADJ
    return mean
