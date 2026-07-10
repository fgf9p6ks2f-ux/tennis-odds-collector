"""Re-grade already-logged spots with the NEW (minutes-honest) model, against the REAL
book lines we logged — the forward test the season-average backtest can't do.

For each graded spot on a date, recompute the projection leak-free (only games strictly
before that date), pick a side vs the REAL line, and grade against the actual box score.
Three strategies on the identical board of real lines:
  OLD          : bet OVER when the raw-elevated projection clears the line   [what we did]
  NEW-directional: bet whichever side the minutes-honest projection favors
  NEW-under    : bet UNDER only, on rebounds+assists, when MH sits >=0.5 below the line
                 (the backtest's edge — overs regress, reb/ast unders are the signal)

    python wnba_regrade.py 2026-07-09
"""
from __future__ import annotations

import sqlite3
import statistics as st
import sys
from pathlib import Path

import wnba_wowy as W

DB = Path(__file__).resolve().parent / "wnba_ledger.sqlite"
STAT = {"points": "pts", "rebounds": "reb", "assists": "ast"}


def _ids():
    """{display_name: athlete_id} from rosters only (no game-log fetch — fast)."""
    out = {}
    teams = W._get(f"{W.SITE}/teams").get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    for t in teams:
        tm = t["team"]
        for a in W._get(f"{W.SITE}/teams/{tm['id']}/roster").get("athletes", []):
            if a.get("id") and a.get("displayName"):
                out[a["displayName"]] = a["id"]
    return out


def project(pid, stat, proj_min, before_date):
    """(old_raw_elevated, minutes_honest, n_elev) from games strictly before `before_date`."""
    log = W.game_log(pid)
    prior = [g for g in log if g["date"][:10] < before_date and g["min"] > 0]
    floor = max(proj_min - 4, 22)
    elev = [g for g in prior if g["min"] >= floor]
    if len(elev) < 3:
        return None
    old = st.mean(g[stat] for g in elev)
    mh = st.mean(g[stat] * min(proj_min / max(g["min"], 1), 1.35) for g in elev)
    return old, mh, len(elev)


def units(dec):
    """Odds-based sizing: flat 1u at <=+100, shrinking with price, floored at 0.25u."""
    if dec <= 2.0:
        return 1.0
    return max(0.25, round((2.0 / dec) ** 1.7 / 0.05) * 0.05)


def won(side, line, actual):
    return (side == "over" and actual > line) or (side == "under" and actual < line)


def apply_sides(date):
    """Write the new model's chosen side back into the ledger for `date`, so the tracker's
    record becomes the NEW (minutes-honest, directional) model's record and continues from
    there. Idempotent — re-run any time; it recomputes leak-free from game logs."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    have = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
    if "side" not in have:
        con.execute("ALTER TABLE predictions ADD COLUMN side TEXT DEFAULT 'over'")
    rows = con.execute("SELECT rowid, player, stat, line, proj_min FROM predictions "
                       "WHERE pred_date=?", (date,)).fetchall()
    idmap = _ids()

    def find_id(name):
        if name in idmap:
            return idmap[name]
        nn = W_norm(name)
        return next((v for k, v in idmap.items() if W_norm(k) == nn), None)

    n = 0
    for r in rows:
        pid, key = find_id(r["player"]), STAT.get(r["stat"])
        if not pid or not key:
            continue
        pr = project(pid, key, r["proj_min"] or 24.0, date)
        if not pr:
            continue
        _old, mh, _n = pr
        side = "over" if mh >= r["line"] else "under"
        n += con.execute("UPDATE predictions SET side=? WHERE rowid=?", (side, r["rowid"])).rowcount
    con.commit()
    con.close()
    return n


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    date = args[0] if args else "2026-07-09"
    if "--apply" in sys.argv:
        n = apply_sides(date)
        print(f"applied new-model sides to {n} ledger rows on {date} "
              f"(tracker now reflects the new model). Re-run --report to see the record.")
        return
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM predictions WHERE pred_date=? AND graded=1 "
                       "ORDER BY player, stat, line", (date,)).fetchall()
    idmap = _ids()

    def find_id(name):
        if name in idmap:
            return idmap[name]
        nn = W_norm(name)
        for k, v in idmap.items():
            if W_norm(k) == nn:
                return v
        return None

    strat = {"OLD": [], "NEW-dir": [], "NEW-under": []}   # (name, stat, line, side, win, dec)
    print(f"\nRE-GRADE {date} — new model vs the REAL logged lines\n")
    print(f"  {'player':17}{'stat':4}{'line':>6}{'act':>5}{'old':>6}{'MH':>6}"
          f"   OLD -> NEW-dir")
    for r in rows:
        name, stat = r["player"], r["stat"]
        key = STAT.get(stat)
        pid = find_id(name)
        if not pid or not key:
            continue
        pr = project(pid, key, r["proj_min"] or 24.0, date)
        if not pr:
            continue
        old, mh, _n = pr
        line, actual, dec = r["line"], r["actual"], r["odds"] or 1.91

        old_side = "over" if old > line else None                     # old bet overs only
        dir_side = "over" if mh > line else "under"                    # new: follow MH
        und_ok = stat in ("rebounds", "assists") and mh <= line - 0.5  # new-under strategy
        und_side = "under" if und_ok else None

        if old_side:
            strat["OLD"].append((name, stat, line, old_side, won(old_side, line, actual), dec))
        strat["NEW-dir"].append((name, stat, line, dir_side, won(dir_side, line, actual), dec))
        if und_side:
            strat["NEW-under"].append((name, stat, line, und_side, won(und_side, line, actual), dec))

        mark = "WIN " if won(dir_side, line, actual) else "loss"
        oldm = ("over WIN " if old_side and won(old_side, line, actual)
                else "over loss" if old_side else "—        ")
        print(f"  {name[:16]:17}{stat[:3]:4}{line:>6.1f}{actual:>5.0f}{old:>6.1f}{mh:>6.1f}"
              f"   {oldm} -> {dir_side} {mark}")

    print(f"\n  {'strategy':12}{'bets':>6}{'W-L':>8}{'hit%':>7}{'units P&L':>11}")
    for s, bets in strat.items():
        if not bets:
            continue
        w = sum(1 for b in bets if b[4])
        pnl = sum((units(b[5]) * (b[5] - 1) if b[4] else -units(b[5])) for b in bets)
        print(f"  {s:12}{len(bets):>6}{f'{w}-{len(bets)-w}':>8}"
              f"{100*w/len(bets):>6.0f}%{pnl:>+11.2f}u")
    print("\n  (ladder rungs on the same player-stat are correlated — treat as one lean, not N bets)")


def W_norm(s):
    return "".join(ch for ch in s.lower().replace("ł", "l") if ch.isalnum())


if __name__ == "__main__":
    main()
