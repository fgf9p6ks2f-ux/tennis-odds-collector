#!/usr/bin/env python3
"""One-time (idempotent) backfill of predictions.odds_other for rows logged BEFORE we
started storing both sides of the price natively (2026-07-13). odds_other = the OPPOSITE
side's price at flag time; it's what lets side-flips (the role_flip OVER) and no-vig
fair-value be backtested from real posted numbers instead of forward-only.

Source: fd_lines (fanduel_props.sqlite) = the raw FanDuel price history, which carries
BOTH sides at the EXACT bet line for every stat INCLUDING combos (pts_ast/reb_ast/pra/
pts_reb). We take the EARLIEST price on the slate date = the opening = the number that
was available when we flagged the bet.

Why not the CLV shadow (the originally-suggested source): it only logs the three BASE
stats (points/rebounds/assists) at the book's MAIN line, so it misses the combo unders
the flip actually targets — on this ledger it could fill exactly 1 row; fd_lines fills 59.

Rerunnable: only touches rows where odds_other IS NULL. Where the book never posted the
opposite side at that line (extreme alt lines), the price is genuinely absent and the row
is correctly left NULL.
"""
import datetime
import sqlite3
from pathlib import Path

import wnba_ledger as L

HERE = Path(__file__).resolve().parent
PROPS = HERE / "fanduel_props.sqlite"


def _plus(d, k):
    return (datetime.date.fromisoformat(d) + datetime.timedelta(days=k)).isoformat()


def _paired_opp(fp, player, stat, line, side, our_odds, date):
    """The OPPOSITE side's price at the exact line, taken from a snapshot where BOTH sides
    were posted at the SAME instant (a genuine two-sided market) — never a lone one-sided
    alt-line phantom. Prefer the snapshot whose OUR-side price equals what we logged (= our
    actual flag moment); else the earliest real two-sided market. Returns None if the book
    never posted a two-sided market at our line (then the opposite price is genuinely absent
    and the row stays NULL rather than pairing with a temporally-mismatched phantom)."""
    rows = fp.execute(
        "SELECT collected_at, side, odds FROM fd_lines WHERE sport='wnba' AND player=? "
        "AND stat=? AND ROUND(line,1)=ROUND(?,1) AND substr(collected_at,1,10) BETWEEN ? AND ? "
        "ORDER BY collected_at ASC",
        (player, stat, line, _plus(date, -1), _plus(date, 1))).fetchall()
    our = side or "over"
    opp = "over" if our == "under" else "under"
    snaps, cur_ca, cur = [], None, None
    for ca, sd, od in rows:
        if ca != cur_ca:
            cur_ca, cur = ca, {}
            snaps.append(cur)
        cur[sd] = od
    twosided = [d for d in snaps if our in d and opp in d]
    if not twosided:
        return None
    for d in twosided:                        # our-side price == logged price -> the flag moment
        if abs(d[our] - our_odds) < 1e-6:
            return d[opp]
    return twosided[0][opp]                    # else the earliest genuine two-sided market


def main():
    if not PROPS.exists():
        print(f"backfill_odds_other: {PROPS.name} not found — nothing to do.")
        return
    con = L._con()                       # applies the odds_other migration if missing
    con.row_factory = sqlite3.Row
    fp = sqlite3.connect(PROPS)
    need = con.execute(
        "SELECT pred_date, player, stat, line, side, odds FROM predictions WHERE odds_other IS NULL"
    ).fetchall()
    filled = absent = 0
    for r in need:
        px = _paired_opp(fp, r["player"], r["stat"], r["line"], r["side"], r["odds"], r["pred_date"])
        if px is None:
            absent += 1
            continue
        con.execute(
            "UPDATE predictions SET odds_other=? "
            "WHERE pred_date=? AND player=? AND stat=? AND line=?",
            (px, r["pred_date"], r["player"], r["stat"], r["line"]))
        filled += 1
    con.commit()
    con.close()
    fp.close()
    print(f"backfill_odds_other: {len(need)} NULL rows -> {filled} filled from fd_lines "
          f"openings, {absent} left NULL (book never posted the opposite side at that line).")


if __name__ == "__main__":
    main()
