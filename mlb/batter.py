"""Batter total-bases model.

A batter's total bases in a game = sum over plate appearances of bases-per-PA. We build
the per-PA outcome distribution (0 / 1B / 2B / 3B / HR) from the batter's rate stats,
adjust it for the opposing staff, the platoon (starter's hand) and park, then convolve it
over a Poisson number of PAs to get the FULL total-bases distribution -> P(over any line).
That full distribution is what lets us price FanDuel's 2.5 / 3.5 / 4.5 alt lines, which
Pinnacle doesn't post directly.

Consistent-loop interface (mirrors strikeouts.py / outs.py):
    project(line, pitcher_factor, hand_factor, ...) -> Projection(mean, pmf)
    prob_over(proj, line)                           -> P(TB > line)
    fair_odds(p)                                    -> (over_dec, under_dec)
"""
from __future__ import annotations

from dataclasses import dataclass
from math import exp, factorial

# league per-PA rates (1B / 2B / 3B / HR), ~modern run environment; refined from data in
# the backtest. Sum ~= hits/PA ~= .218; mean TB/PA ~= .37.
LG_RATES = {1: 0.136, 2: 0.045, 3: 0.004, 4: 0.033}
PRIOR_PA = 200        # shrink a batter's rates toward league with this many prior PA
EXP_PA = 4.3          # default plate appearances per game
MAX_FACTOR = 2.5      # clamp matchup multipliers so a soft spot can't explode a rate


@dataclass
class Projection:
    mean: float
    pmf: dict          # {total_bases: probability}


def per_pa_rates(line: dict) -> dict | None:
    """Per-PA {1:1B, 2:2B, 3:3B, 4:HR} probabilities from a hitting line."""
    pa = line.get("pa") or 0
    if pa <= 0:
        return None
    singles = line["h"] - line["2b"] - line["3b"] - line["hr"]
    return {1: max(singles, 0) / pa, 2: line["2b"] / pa,
            3: line["3b"] / pa, 4: line["hr"] / pa}


def shrink(rates: dict, pa: float, prior: float = PRIOR_PA, lg: dict = LG_RATES) -> dict:
    """Regress thin samples toward league — a 30-PA hot streak isn't a true rate."""
    w = pa / (pa + prior)
    return {k: w * rates[k] + (1 - w) * lg[k] for k in lg}


def pa_for_slot(slot: int) -> float:
    """Expected plate appearances by lineup position (1-9). Leadoff bats ~4.6, #9 ~3.9."""
    return {1: 4.65, 2: 4.55, 3: 4.45, 4: 4.35, 5: 4.25,
            6: 4.15, 7: 4.05, 8: 3.95, 9: 3.85}.get(slot, EXP_PA)


def exp_pa_from(line: dict, lo: float = 2.4, hi: float = 4.7) -> float:
    """Expected plate appearances from the batter's season PA/game — respects a part-time
    role, so a bench bat isn't projected like an everyday starter."""
    gp = line.get("gp") or 0
    return min(max(line["pa"] / gp, lo), hi) if gp else EXP_PA


def _poisson(k: int, lam: float) -> float:
    return exp(-lam) * lam ** k / factorial(k)


def _pa_pmf(rates: dict) -> dict:
    """Total-bases pmf for a single plate appearance."""
    return {0: 1 - sum(rates.values()), 1: rates[1], 2: rates[2], 3: rates[3], 4: rates[4]}


def _convolve(a: dict, b: dict) -> dict:
    out: dict = {}
    for k1, v1 in a.items():
        if v1 <= 0:
            continue
        for k2, v2 in b.items():
            out[k1 + k2] = out.get(k1 + k2, 0.0) + v1 * v2
    return out


def tb_pmf(rates: dict, exp_pa: float = EXP_PA, max_pa: int = 8) -> dict:
    """Full total-bases pmf: Poisson(exp_pa) plate appearances, per-PA TB convolved."""
    single = _pa_pmf(rates)
    total: dict = {}
    cur = {0: 1.0}                      # convolution of 0 PAs
    wsum = 0.0
    for n in range(max_pa + 1):
        w = _poisson(n, exp_pa)
        wsum += w
        for tb, p in cur.items():
            total[tb] = total.get(tb, 0.0) + w * p
        cur = _convolve(cur, single)
    return {k: v / wsum for k, v in total.items()}   # renormalize dropped tail


def project(line: dict, pitcher_factor: float = 1.0, hand_factor: float = 1.0,
            park_factor: float = 1.0, exp_pa: float = EXP_PA,
            lg: dict = LG_RATES) -> Projection:
    """Adjust a batter's per-PA rates for the matchup and build the TB distribution."""
    base = per_pa_rates(line)
    if base is None:
        raise ValueError("no plate appearances")
    base = shrink(base, line["pa"], lg=lg)
    f = min(pitcher_factor * hand_factor * park_factor, MAX_FACTOR)
    adj = {k: base[k] * f for k in base}
    s = sum(adj.values())
    if s >= 0.95:                      # keep P(no total base) strictly positive
        adj = {k: v * 0.95 / s for k, v in adj.items()}
    pmf = tb_pmf(adj, exp_pa)
    return Projection(mean=sum(tb * p for tb, p in pmf.items()), pmf=pmf)


def prob_over(proj: Projection, line: float) -> float:
    """P(total bases > line), e.g. line 1.5 -> P(TB >= 2)."""
    return sum(p for tb, p in proj.pmf.items() if tb > line)


def anchor(line: dict, line0: float, p0: float, exp_pa: float = EXP_PA,
           lg: dict = LG_RATES) -> Projection:
    """Anchor the model's LEVEL to a sharp market line: scale the batter's per-PA rates by
    a factor phi so that P(TB > line0) == p0 (Pinnacle's de-vigged main-line probability).
    The model then supplies only the DISTRIBUTION SHAPE for the alt lines Pinnacle doesn't
    post — we trust the sharp book for the hard part (level) and the model for curvature.
    """
    base = shrink(per_pa_rates(line), line["pa"], lg=lg)

    def p_over(phi):
        r = {k: base[k] * phi for k in base}
        s = sum(r.values())
        if s >= 0.95:
            r = {k: v * 0.95 / s for k, v in r.items()}
        return sum(p for tb, p in tb_pmf(r, exp_pa).items() if tb > line0)

    lo, hi = 0.2, 4.0                  # p_over is monotincreasing in phi -> bisect
    for _ in range(40):
        mid = (lo + hi) / 2
        if p_over(mid) < p0:
            lo = mid
        else:
            hi = mid
    phi = (lo + hi) / 2
    adj = {k: base[k] * phi for k in base}
    s = sum(adj.values())
    if s >= 0.95:
        adj = {k: v * 0.95 / s for k, v in adj.items()}
    pmf = tb_pmf(adj, exp_pa)
    return Projection(mean=sum(tb * p for tb, p in pmf.items()), pmf=pmf)


def fair_odds(p: float) -> tuple[float, float]:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return round(1 / p, 3), round(1 / (1 - p), 3)
