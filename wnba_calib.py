#!/usr/bin/env python3
"""Calibration + discrimination monitor for the WNBA overs model — the data-triggered green light
for expanding props / edge-sizing.

Two very different questions, both measured off the graded overs:
  LEVEL (calibration): when the model says 63%, does ~63% happen? -> the OPTIMISM gap. Fixable by a
    haircut; cosmetic (rescaling preserves the order).
  RANKING (discrimination): do the bets it rates HIGHER actually win MORE? -> hi-conf vs lo-conf
    realized hit-rate. THIS is what Kelly/edge-sizing and expanding props require — a model that
    can't rank its own bets can't be sized by edge, and fanning out to marginal spots just dilutes
    a thin aggregate edge with variance.

So we only turn GREEN (expand + edge-size) once, at a real sample, the higher-confidence bets
genuinely win more. Until then: flat sizing + current selection. (2026-07-14 baseline: 33 overs,
+8.4% optimism, discrimination -9% i.e. hi-conf hit LESS — no green light.)
"""
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"

MIN_N = 100         # graded overs before a hi/lo-half discrimination split is worth trusting
GAP_BAR = 0.10      # hi-conf minus lo-conf realized hit-rate that counts as real ranking (>~1 SE)
BREAKEVEN = 0.524   # -110 two-way


def calibration(epoch="2026-07-09"):
    """Returns the calibration/discrimination snapshot, or None if no graded overs."""
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT ev, odds, result FROM predictions WHERE side='over' AND result IN ('over','under') "
        "AND ev IS NOT NULL AND odds IS NOT NULL AND pred_date>=?", (epoch,))]
    con.close()
    n = len(rows)
    if n == 0:
        return None
    for r in rows:
        r["p"] = (r["ev"] + 1) / r["odds"]           # model's shrunk P(over), the number EV is built on
        r["win"] = 1 if r["result"] == "over" else 0
    realized = sum(r["win"] for r in rows) / n
    optimism = sum(r["p"] for r in rows) / n - realized
    srt = sorted(rows, key=lambda r: -r["p"])
    h = n // 2
    top = sum(r["win"] for r in srt[:h]) / h if h else 0.0
    bot = sum(r["win"] for r in srt[h:]) / (n - h) if n - h else 0.0
    disc = top - bot                                 # >0 = higher-confidence bets win more (good)
    ready = n >= MIN_N
    if not ready:
        verdict = f"accumulating {n}/{MIN_N} graded — flat sizing + current selection (no expand)"
    elif disc >= GAP_BAR and realized > BREAKEVEN:
        verdict = "GREEN — model ranks its bets; consider edge-sizing + expanding props"
    else:
        verdict = "HOLD — no ranking edge; keep flat sizing, do NOT expand"
    return {"n": n, "realized": realized, "edge": realized - BREAKEVEN, "optimism": optimism,
            "disc": disc, "top": top, "bot": bot, "ready": ready, "verdict": verdict}


def report():
    c = calibration()
    if not c:
        print("calibration monitor: no graded overs yet")
        return
    print(f"CALIBRATION MONITOR ({c['n']} graded overs):")
    print(f"  aggregate over-rate {c['realized']:.1%}  (breakeven {BREAKEVEN:.1%}, edge {c['edge']:+.1%})")
    print(f"  LEVEL   — optimism (pred - real): {c['optimism']:+.1%}")
    print(f"  RANKING — hi-conf {c['top']:.0%} vs lo-conf {c['bot']:.0%}  = {c['disc']:+.0%} discrimination")
    print(f"  -> {c['verdict']}")


if __name__ == "__main__":
    report()
