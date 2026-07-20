"""POINTS-OVER SELECTION AUDIT + haircut shadow (2026-07-19, user).

Question that started this: points overs looked like a money-loser (13-18, -5.31u). Is a
projection HAIRCUT the fix? Audit answer: NO — the loss is almost entirely LEGACY out-of-band
plays the current model already refuses to bet. Split the same 31 graded points overs by what
TODAY's model does with them:

    current model BETS (d_min in [0,8] or cold None):  ~12-9 (57%)  +2.8u   <- profitable
    current model SHADOWS (out-of-band <0 or >8):      ~1-9  (10%)  -8.1u   <- the whole loss

So the 7/18 band gate already plugged the leak. A blanket haircut would just shave the plays
that are already winning. What DOES separate winners from losers inside the bet set is the size
of the projected role jump (elevation over season avg): a MODERATE +3-5 bump lands ~88%, while
both a marginal (<3) and a "model-dreaming" (>=5) bump underperform — but those buckets are n=2-8,
so it's a forward hypothesis, not a gate.

This module changes NO live flag/EV/bet. Re-run at the checkpoint; it reads the live ledger.

    python3 wnba_points_haircut.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"

# the haircut menu was FIT to points plays graded on/before this date -> in-sample/circular. Plays
# graded AFTER are the honest out-of-sample test.
EPOCH = "2026-07-19"

HAIRCUTS = {
    "x0.90": lambda p: p * 0.90,
    "x0.87": lambda p: p * 0.87,
    "x0.84": lambda p: p * 0.84,
    "-1.5":  lambda p: p - 1.5,
    "-2.5":  lambda p: p - 2.5,
}


def points_haircut(proj, level="x0.87"):
    """Candidate live haircut — NOT wired into projections. The audit found the band gate already
    handles the points leak, so this stays parked unless the forward sample says otherwise."""
    return HAIRCUTS.get(level, lambda p: p)(proj)


def _rec(rs):
    w = sum(1 for r in rs if r["result"] == "over")
    n = len(rs)
    u = sum((r["odds"] - 1) if r["result"] == "over" else -1 for r in rs)
    return f"{w}-{n-w} ({w/n*100:.0f}%) {u:+.2f}u" if n else "n=0"


def _overs(where="1=1"):
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT pred_date,player,stat,line,odds,elev_avg,season_avg,d_min,d_fga,result "
        "FROM predictions WHERE (side IS NULL OR side='over') AND result IN ('over','under') "
        f"AND ({where})")]
    con.close()
    return rows


def per_prop():
    rows = _overs()
    byp = defaultdict(list)
    for r in rows:
        byp[r["stat"]].append(r)
    print("PER-PROP RECORD (overs, graded)")
    for s in ["points", "rebounds", "assists", "pts_reb", "pts_ast", "reb_ast", "pra"]:
        print(f"  {s:10} {_rec(byp.get(s, []))}")
    print(f"  {'ALL':10} {_rec(rows)}")


def points_regime():
    pts = _overs("stat='points' AND elev_avg IS NOT NULL")
    inband = [r for r in pts if r["d_min"] is not None and 0 <= r["d_min"] <= 8]
    cold = [r for r in pts if r["d_min"] is None]
    oob = [r for r in pts if r["d_min"] is not None and (r["d_min"] < 0 or r["d_min"] > 8)]
    print("POINTS overs by what the CURRENT model does with them")
    print(f"  full historical sample:            {_rec(pts)}")
    print(f"  CURRENT MODEL BETS (in-band+cold): {_rec(inband + cold)}")
    print(f"    in-band d_min [0,8]:             {_rec(inband)}")
    print(f"    cold d_min=None:                 {_rec(cold)}")
    print(f"  SHADOWED, not bet (out-of-band):   {_rec(oob)}   <- the whole loss lives here")


def elevation():
    # residual signal INSIDE the bet set: how big a jump over season avg does the projection demand?
    pts = [r for r in _overs("stat='points' AND elev_avg IS NOT NULL")
           if r["d_min"] is not None and 0 <= r["d_min"] <= 8 and r["season_avg"] is not None]
    print("ELEVATION (proj - season_avg) inside the bet set  [small n — forward hypothesis]")
    for lo, hi, lbl in [(-99, 3, "elev <3  (marginal)"),
                        (3, 5, "elev 3-5 (believable)"),
                        (5, 99, "elev >=5 (model dreaming)")]:
        g = [r for r in pts if lo <= (r["elev_avg"] - r["season_avg"]) < hi]
        print(f"  {lbl:26} {_rec(g)}")


def haircut_menu():
    pts = _overs("stat='points' AND elev_avg IS NOT NULL")
    fwd = [r for r in pts if (r["pred_date"] or "") >= EPOCH]
    print(f"HAIRCUT SHADOW MENU (parked — kept for the forward test; drop = haircut proj <= line)")
    for slc, lbl in [(pts, "full (in-sample-heavy)"), (fwd, f"forward >= {EPOCH}")]:
        if not slc:
            print(f"  {lbl}: n=0")
            continue
        print(f"  {lbl}: no-haircut {_rec(slc)}")
        for name, fn in HAIRCUTS.items():
            kept = [r for r in slc if fn(r["elev_avg"]) > r["line"]]
            print(f"    {name:>6} kept {_rec(kept)}")


def report():
    print("=" * 68)
    per_prop()
    print("-" * 68)
    points_regime()
    print("-" * 68)
    elevation()
    print("-" * 68)
    haircut_menu()
    print("=" * 68)
    print("VERDICT: band gate already handles the points leak; haircut is parked.")
    print("WATCH forward: the moderate-elevation sweet spot (established scorer + real")
    print("usage jump + believable +3-5 bump) = the user's own winning archetype.")


if __name__ == "__main__":
    report()
