"""POINTS-PROJECTION HAIRCUT — shadow test (2026-07-20, user).

Per-stat accuracy audit (82 graded flags): the model's POINTS projection runs OPTIMISTIC —
mean projected 15.4 vs actual 12.8 (+2.6), and the player finished UNDER the projection 81%
of the time. Assists (+0.3) and rebounds (+1.3) are far better calibrated. So points-overs are
where the fat-EV traps live.

This module is a SHADOW TEST — it changes NO live flag, EV, or bet. It defines candidate haircut
functions and, run against the graded ledger, reports what each haircut WOULD have done: which
points plays it drops (projection no longer clears the line) and whether those dropped plays were
mostly LOSERS (haircut helps) or winners (haircut hurts). Re-run anytime; it accumulates as new
points plays grade. Promote a level to live only if it beats the no-haircut record over a real
forward sample (~30+ points plays).

    python3 wnba_points_haircut.py        # shadow report on the current ledger
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"

# the haircut menu below was FIT to points plays graded on/before this date, so their improvement is
# in-sample (circular). Plays graded AFTER the epoch are the honest out-of-sample forward test — the
# only record that should decide a live promotion.
EPOCH = "2026-07-19"

# candidate haircuts (proportional shrink dominates — bigger projections over-shoot more; a couple
# of constants for comparison). NONE is applied to live projections; this is the shadow menu.
HAIRCUTS = {
    "none":  lambda p: p,
    "x0.90": lambda p: p * 0.90,
    "x0.87": lambda p: p * 0.87,
    "x0.84": lambda p: p * 0.84,
    "-1.5":  lambda p: p - 1.5,
    "-2.5":  lambda p: p - 2.5,
}


def points_haircut(proj, level="x0.87"):
    """The candidate live haircut (default x0.87 ≈ splitting the measured +2.6 bias, conservative
    vs the in-sample 0.83). NOT wired into projections yet — exported for a future promotion."""
    return HAIRCUTS.get(level, HAIRCUTS["none"])(proj)


def _points_rows():
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT pred_date, player, line, odds, elev_avg, actual, result FROM predictions "
        "WHERE stat='points' AND (side IS NULL OR side='over') AND result IN ('over','under') "
        "AND elev_avg IS NOT NULL")]
    con.close()
    return rows


def _table(rows, label):
    """Print the keep/drop table for one slice of points plays."""
    n = len(rows)
    if not n:
        print(f"  {label}: no graded points plays yet")
        return
    base_w = sum(1 for r in rows if r["result"] == "over")
    print(f"  {label}: {n} plays · no-haircut {base_w}-{n-base_w} ({base_w/n*100:.0f}%)")
    print(f"  {'haircut':>7} {'drops':>6} {'DROPPED W-L':>12} {'KEPT W-L':>10} {'KEPT hit%':>9} {'net Δ':>7}")
    for name, fn in HAIRCUTS.items():
        if name == "none":
            continue
        # DROPPED when the haircut projection no longer clears the line (model would no longer
        # project an over -> the marginal edge evaporates). KEPT plays are the survivors.
        dropped = [r for r in rows if fn(r["elev_avg"]) <= r["line"]]
        kept = [r for r in rows if fn(r["elev_avg"]) > r["line"]]
        dw = sum(1 for r in dropped if r["result"] == "over")
        kw = sum(1 for r in kept if r["result"] == "over")
        kept_pct = f"{kw/len(kept)*100:.0f}%" if kept else "—"
        net = (kw/len(kept) - base_w/n) * 100 if kept else 0
        print(f"  {name:>7} {len(dropped):>6} {dw}-{len(dropped)-dw:>9} "
              f"{kw}-{len(kept)-kw:>7} {kept_pct:>9} {net:>+6.0f}pt")


def shadow_report():
    rows = _points_rows()
    if not rows:
        print("no graded points plays yet")
        return
    insample = [r for r in rows if (r["pred_date"] or "") < EPOCH]
    forward = [r for r in rows if (r["pred_date"] or "") >= EPOCH]
    print("POINTS-PROJECTION HAIRCUT · shadow test (changes no live flag)\n")
    print(f"IN-SAMPLE (graded < {EPOCH} — the fitted set; improvement here is expected/circular)")
    _table(insample, "in-sample")
    print(f"\nFORWARD (graded >= {EPOCH} — the honest out-of-sample test; THIS decides promotion)")
    _table(forward, "forward")
    print("\ngood haircut = DROPPED mostly losers + KEPT hit% ABOVE the base.")
    print("promote a level to live only if it beats no-haircut over the FORWARD sample (~30+ plays).")


if __name__ == "__main__":
    shadow_report()
