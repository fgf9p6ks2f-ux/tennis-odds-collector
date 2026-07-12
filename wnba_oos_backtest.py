#!/usr/bin/env python3
"""Out-of-sample backtest of the shrink_k calibration.

The in-sample sweep (/tmp/shrink_test.py) is circular: k was chosen on the same
graded bets it's scored against, so it CANNOT prove forward lift. This script
does a real temporal split instead:

    * fit k on the EARLIER slates (train)
    * score ONLY the held-out most-recent slates (test) with that fitted k
    * compare fitted-k vs shipped(11/14) vs old(6/9) on the held-out set

The honest question it answers: "if we'd calibrated on the past, would that
calibration have helped on slates it never saw?" It gets trustworthy as forward
graded bets accumulate — re-run it after each new slate.

Usage:
    python wnba_oos_backtest.py                # hold out the 2 most-recent slates
    python wnba_oos_backtest.py --holdout 3    # hold out the 3 most-recent slates
    python wnba_oos_backtest.py --since 2026-07-11   # test = bets on/after this date
"""
import argparse
import pathlib
import sqlite3
import statistics as st
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import dashboard as D  # noqa: E402

# ── config the script is TESTING ───────────────────────────────────────────
OLD = {"elevated": 6, "projected": 9}       # pre-shrinkage baseline
SHIPPED = {"elevated": 11, "projected": 14}  # what's live now (commit 00926ec)
# fit sweeps a single scalar multiplier on OLD so the per-basis ratio is held
# fixed — one free parameter is all a ~25-bet train set can honestly support.
SWEEP = (1.0, 1.3, 1.6, 1.9, 2.2, 2.6, 3.0, 3.6)


def BAR(side, basis):
    """EV bar to clear, matching wnba_tonight: unders easiest, volume-overs mid, else strict."""
    return 0.04 if side == "under" else (0.07 if basis == "volume" else 0.10)


def cfg_for(mult):
    return {k: v * mult for k, v in OLD.items()}


def betset(rows, cfg):
    """The bets that clear their EV bar under a given shrink config, with shrunk P(win)."""
    out = []
    for r in rows:
        k = cfg.get(r["basis"], cfg["projected"])
        p = (r["proj_hit"] * r["n_elev"] + (1 / r["odds"]) * k) / (r["n_elev"] + k)
        if p * r["odds"] - 1 >= BAR(r["side"], r["basis"]):
            out.append((r, p))
    return out


def score(bs):
    """(#bets, wins, realized_hit%, mean_model_p, units, roi%) for a bet list."""
    if not bs:
        return None
    won = sum(1 for r, p in bs if r["result"] == r["side"])
    u = sum(D._units(r["odds"]) * (r["odds"] - 1) if r["result"] == r["side"] else -D._units(r["odds"]) for r, p in bs)
    stake = sum(D._units(r["odds"]) for r, p in bs)
    return len(bs), won, won / len(bs), st.mean(p for r, p in bs), u, 100 * u / stake


def line(label, s):
    if not s:
        print(f"  {label:16} no bets clear the bar"); return
    n, w, real, mp, u, roi = s
    print(f"  {label:16} {n:>3} bets | {w}-{n - w} ({real * 100:.0f}%) | ROI {roi:+.1f}% ({u:+.1f}u) "
          f"| model P {mp * 100:.0f}% vs real {real * 100:.0f}% (gap {(real - mp) * 100:+.0f}p)")


def fit_mult(train):
    """Pick the multiplier that best calibrates the TRAIN slates.

    Objective: minimise |realized - model_p| (over-confidence is the disease we're
    treating). Tie-break on higher ROI. Returns (mult, its train score)."""
    best = None
    for m in SWEEP:
        s = score(betset(train, cfg_for(m)))
        if not s:
            continue
        n, w, real, mp, u, roi = s
        key = (abs(real - mp), -roi)  # smaller gap first, then higher ROI
        if best is None or key < best[0]:
            best = (key, m, s)
    return (best[1], best[2]) if best else (None, None)


def _load_graded(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT proj_hit,n_elev,odds,side,basis,result,pred_date FROM predictions "
        "WHERE graded=1 AND result IN ('over','under') AND proj_hit IS NOT NULL AND n_elev>0")]
    con.close()
    return rows


def _verdict(old, ship, fit):
    """One-line actionable read of the held-out result."""
    gap = lambda s: abs(s[2] - s[3])  # |realized - model_p|
    if fit and ship and fit[5] > ship[5] + 3 and gap(fit) < gap(ship):
        return "harder shrink STILL helps OOS -> consider bumping k"
    best_roi = max(s[5] for s in (old, ship, fit) if s)
    if best_roi < 0:
        return "still losing after best calibration -> issue is PROJECTIONS, not sizing"
    if ship[5] >= 0 and gap(ship) < 0.08:
        return "calibrated & profitable OOS -> healthy"
    return "mixed -- keep measuring"


def digest_lines(path, min_slates=6, holdout=2):
    """Compact OOS calibration read for the nightly digest. Stays SILENT (returns [])
    until there are >= min_slates graded slates, so it only speaks with enough forward
    data to mean something. Never raises — the digest must not die on this."""
    try:
        rows = _load_graded(path)
        slates = sorted(set(r["pred_date"] for r in rows))
        if len(slates) < min_slates:
            return []
        cutoff = slates[-holdout]
        train = [r for r in rows if r["pred_date"] < cutoff]
        test = [r for r in rows if r["pred_date"] >= cutoff]
        if not train or not test:
            return []
        mult, _ = fit_mult(train)
        s_ship = score(betset(test, SHIPPED))
        s_old = score(betset(test, OLD))
        s_fit = score(betset(test, cfg_for(mult))) if mult else None
        if not s_ship:
            return []
        n, w, real, mp, u, roi = s_ship
        out = [f"CALIBRATION (out-of-sample: {len(slates) - holdout} train / {holdout} test slates, {n} held-out bets):",
               f"  shipped k=11/14: {w}-{n - w}, ROI {roi:+.0f}%, over-conf gap {(real - mp) * 100:+.0f}p",
               f"  -> {_verdict(s_old, s_ship, s_fit)}"]
        return out
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", type=int, default=2, help="# most-recent slates to hold out as test")
    ap.add_argument("--since", type=str, default=None, help="explicit test cutoff YYYY-MM-DD (overrides --holdout)")
    args = ap.parse_args()

    # freshest committed ledger, graded in a throwaway copy
    tmp = pathlib.Path("/tmp/oos_led.sqlite")
    tmp.write_bytes(subprocess.run(["git", "show", "origin/main:wnba_ledger.sqlite"],
                                   cwd=pathlib.Path(__file__).resolve().parent, capture_output=True).stdout)
    import wnba_ledger as L
    L.DB = tmp
    L.grade()
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    g = [dict(r) for r in c.execute(
        "SELECT proj_hit,n_elev,odds,side,basis,result,pred_date FROM predictions "
        "WHERE graded=1 AND result IN ('over','under') AND proj_hit IS NOT NULL AND n_elev>0")]

    slates = sorted(set(r["pred_date"] for r in g))
    if args.since:
        cutoff = args.since
    else:
        if len(slates) <= args.holdout:
            print(f"only {len(slates)} graded slates — need > {args.holdout} to split. "
                  f"Add forward data, then re-run.")
            return
        cutoff = slates[-args.holdout]

    train = [r for r in g if r["pred_date"] < cutoff]
    test = [r for r in g if r["pred_date"] >= cutoff]

    print(f"graded bets: {len(g)} across {len(slates)} slates ({slates[0]}..{slates[-1]})")
    print(f"split: TRAIN = {len([s for s in slates if s < cutoff])} slates (<{cutoff}), "
          f"TEST = {len([s for s in slates if s >= cutoff])} slates (>={cutoff})\n")

    if not train or not test:
        print("empty side of split — widen the data or adjust --holdout/--since.")
        return

    mult, tr_s = fit_mult(train)
    fitted = cfg_for(mult)
    print(f"FIT on train ({len(train)} bets): best multiplier {mult:.1f}x  "
          f"→ k≈{fitted['elevated']:.0f}/{fitted['projected']:.0f}")
    line("  train@fit", tr_s)
    print()

    print(f"HELD-OUT TEST ({len(test)} bets) — the only numbers that aren't circular:")
    line("old 6/9", score(betset(test, OLD)))
    line("shipped 11/14", score(betset(test, SHIPPED)))
    line(f"fitted {mult:.1f}x", score(betset(test, fitted)))

    n_test = len(test)
    print()
    if n_test < 30:
        print(f"⚠️  PREVIEW ONLY: {n_test} held-out bets is far too thin to trust the ROI — "
              f"calibration gap is the signal to watch, and even that is noisy. Re-run after "
              f"~{max(0, 40 - n_test)}+ more graded bets land.")
    else:
        print(f"held-out n={n_test}: calibration gap is meaningful; ROI still wants ~100+ bets.")


if __name__ == "__main__":
    main()
