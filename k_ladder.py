"""Pitcher-strikeout ALT-LADDER scanner — pure cross-book LINE-SHOPPING.

The backtest verdict (k_backtest.py, 910 starts 2026): the projection model is nearly
unbiased but WEAK (proj-vs-actual corr 0.37) and the K distribution is ~Poisson
(var/mean 1.2) — a model alone CANNOT beat the K market, and pricing a book's UNIQUE
deep rung off our model just bets our own error at the tail (the total_bases trap).

So this scanner uses ZERO model. The only honest edge is the SAME rung priced better at
one book than the others:
  1. group every (pitcher, over-line) posted by >=2 of FD/DK/BetMGM.
  2. fair = the vig-stripped consensus of those books' implied probs (median 1/odds,
     de-vigged by the pitcher's own two-sided main-line hold when available, else ~4%).
  3. flag when the BEST book's price gives EV = fair*odds - 1 >= MIN_EV. That's a real
     line-shop edge — one book is simply paying more for the identical bet.
Rungs only one book posts are shown greyed, never flagged (no consensus = no fair).

Reads fd_lines (stat='strikeouts', all books) written by fd/dk/betmgm collectors.

    python k_ladder.py            # scan tonight's ladders, print + write k_ladder.md
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

from wnba_edge_scan import canon, fair_prob

HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("FD_DB", HERE / "fanduel_props.sqlite"))
REPORT = HERE / "k_ladder.md"
MIN_EV = 0.02            # flag a rung when the best book beats consensus fair by >=2%
DEFAULT_HOLD = 0.045    # one-sided vig to strip when no two-sided main line exists
BOOKS = ("fd", "dk", "betmgm")


def _main_hold(lines):
    """Half the two-sided over-round of the pitcher's main line = per-side vig to strip
    from one-sided alt implied probs. Falls back to DEFAULT_HOLD."""
    pairs = defaultdict(dict)
    for (ln, sd), od in lines.items():
        pairs[ln][sd] = od
    two = {ln: s for ln, s in pairs.items() if "over" in s and "under" in s}
    if not two:
        return DEFAULT_HOLD
    ln = min(two, key=lambda L: abs(two[L]["over"] - two[L]["under"]))
    over_round = 1 / two[ln]["over"] + 1 / two[ln]["under"]
    return max(0.0, (over_round - 1) / 2)


def _latest_k_lines():
    """{(pitcher_canon): {'name':.., 'event':.., 'books':{book:{(line,side):odds}}}}
    from each book's most-recent snapshot."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT collected_at, event, player, line, side, odds, COALESCE(book,'fd') "
        "FROM fd_lines WHERE sport='mlb' AND stat='strikeouts' "
        "AND collected_at > datetime('now','-6 hours')").fetchall()
    con.close()
    latest = defaultdict(str)
    for r in rows:
        latest[r[6]] = max(latest[r[6]], r[0])
    out = defaultdict(lambda: {"name": "", "event": "", "books": defaultdict(dict)})
    for ca, ev, pl, ln, sd, od, bk in rows:
        if ca != latest[bk] or ln is None:
            continue
        p = out[canon(pl)]
        p["name"] = pl
        p["event"] = ev
        p["books"][bk][(round(float(ln), 1), sd)] = float(od)
    return out


def scan():
    pitchers = _latest_k_lines()
    out = []
    for pk, info in pitchers.items():
        # per-book vig to strip: use each book's own main-line hold
        hold = {bk: _main_hold(lines) for bk, lines in info["books"].items()}
        rungs = sorted({ln for bk in info["books"].values() for (ln, sd) in bk
                        if sd == "over"})
        ladder = []
        for ln in rungs:
            quotes = {bk: info["books"][bk][(ln, "over")]
                      for bk in BOOKS if (ln, "over") in info["books"].get(bk, {})}
            if not quotes:
                continue
            implied = {bk: 1 / od for bk, od in quotes.items()}
            # devig: the true prob is BELOW the implied (vig inflates it) -> SUBTRACT the
            # per-side hold. (Adding it was the sign bug that faked +9% on every rung.)
            fairs = [max(1e-3, implied[bk] - hold.get(bk, DEFAULT_HOLD)) for bk in quotes]
            consensus = statistics.median(fairs)
            best_bk = max(quotes, key=quotes.get)
            best_od = quotes[best_bk]
            # a rung is only shoppable when >=2 books AGREE closely — a wide spread on a
            # deep rung means one book is mispriced (usually the generous one), and betting
            # the consensus there just bets that book's error, the total_bases trap again.
            spread_ok = (max(implied.values()) / min(implied.values())) <= 1.20
            shoppable = len(quotes) >= 2 and spread_ok
            ev = consensus * best_od - 1 if shoppable else None
            ladder.append({"line": ln, "books": quotes, "consensus": consensus,
                           "book": best_bk, "odds": best_od, "ev": ev,
                           "shoppable": shoppable, "n_books": len(quotes)})
        flagged = [r for r in ladder if r["ev"] is not None]
        best = max((r["ev"] for r in flagged), default=None)
        if ladder:
            out.append({"pitcher": info["name"], "event": info["event"],
                        "ladder": ladder, "best_ev": best})
    return sorted(out, key=lambda p: (p["best_ev"] is None, -(p["best_ev"] or -9)))


def american(dec):
    return f"+{round((dec-1)*100)}" if dec >= 2 else f"{round(-100/(dec-1))}"


def main():
    ps = scan()
    plus = [p for p in ps if p["best_ev"] is not None and p["best_ev"] >= MIN_EV]
    verdict = (f"**{len(plus)} pitchers with a +{MIN_EV*100:.0f}%-EV shoppable rung** "
               f"of {len(ps)}." if plus else
               "**No +EV rung tonight** — the K market is efficient; even the best of 3 "
               "books is -EV on every agreeing rung. This is a SHOPPING sheet (best book "
               "per rung to minimize vig), not a bet list. A genuine +EV rung (a slow "
               "book) would ⭐ here.")
    lines = ["# Pitcher strikeout alt-ladder scan (cross-book line-shopping)", "",
             f"_{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · fair = vig-stripped "
             "consensus of the books posting each rung. NO projection model (backtest: "
             "corr 0.37, too weak to price K tails; and pricing a book's unique rung off a "
             "model just bets our own error). Only rungs >=2 books agree on can be flagged._",
             "", verdict, ""]
    for p in ps[:25]:
        star = " ⭐" if (p["best_ev"] or -9) >= MIN_EV else ""
        lines.append(f"**{p['pitcher']}**{star}")
        for r in p["ladder"]:
            if r["shoppable"]:
                flag = f"  ✅ +{r['ev']*100:.0f}% EV" if r["ev"] >= MIN_EV else ""
                books = " ".join(f"{b.upper()} {american(o)}" for b, o in
                                 sorted(r["books"].items(), key=lambda x: -x[1]))
                lines.append(f"  O{r['line']:g}: fair {r['consensus']*100:.0f}% · "
                             f"{books} → best {r['book'].upper()}{flag}")
            else:
                b, o = next(iter(r["books"].items()))
                lines.append(f"  O{r['line']:g}: {b.upper()} {american(o)} (1 book, "
                             "no consensus)")
        lines.append("")
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"k_ladder: {len(ps)} pitchers, {len(plus)} with a +EV shoppable rung "
          f"-> {REPORT.name}")
    for p in plus[:12]:
        r = max((x for x in p["ladder"] if x["ev"] is not None), key=lambda x: x["ev"])
        allbooks = " ".join(f"{b.upper()} {american(o)}" for b, o in
                            sorted(r["books"].items(), key=lambda x: -x[1]))
        print(f"  {p['pitcher']:20} O{r['line']:g}: {allbooks}  fair "
              f"{r['consensus']*100:.0f}%  +{r['ev']*100:.0f}% EV")


if __name__ == "__main__":
    main()
