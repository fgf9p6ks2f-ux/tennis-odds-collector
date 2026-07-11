"""CLV SHADOW-LOGGER — the proof-of-timing-edge loop.

The real edge isn't "beat the settled line" (the book prices injury roles correctly by tip). It's
BEATING THE BOOK TO THE REPRICE: an injury hits, we instantly know the replacement + project their
minutes/production, and we'd bet the over BEFORE the line moves up to reflect it. This measures
exactly that, without risking money: at the MOMENT we first flag an injury-driven projection, log
our number + the line then available. At/after the close, capture the closing line. CLV = did the
line move the way our projection said it would?

If (proj - flag_line) predicts (close_line - flag_line) — i.e. when we say "the line is too low"
the line then rises — the timing edge is REAL and the autobetter has its green light. If not, it
isn't, and we learn that from data instead of a losing bet.

    python wnba_clv.py --close     # capture closing lines for open shadows
    python wnba_clv.py --grade     # box-score actuals
    python wnba_clv.py --report    # CLV summary -> wnba_clv.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import statistics as st
from pathlib import Path

import wnba_wowy as W

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_clv.sqlite"
REPORT = HERE / "wnba_clv.md"
PROPS_DB = HERE / "fanduel_props.sqlite"
STAT_KEY = {"points": "pts", "rebounds": "reb", "assists": "ast"}
MIN_GRADED = 20

SCHEMA = """CREATE TABLE IF NOT EXISTS clv(
  date TEXT, player TEXT, stat TEXT, out_player TEXT, flagged_at TEXT, proj REAL,
  flag_line REAL, flag_over REAL, close_line REAL, close_over REAL, closed INTEGER DEFAULT 0,
  actual REAL, graded INTEGER DEFAULT 0, UNIQUE(date, player, stat));"""


def _con():
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    return con


def nearest_over(ladder, proj):
    """The over we'd actually BET: the posted rung closest to our projection with a usable over
    price. This is stable even on a THIN early ladder (we only need one rung near proj), unlike the
    balanced 'main line' which needs a mature ladder — and thin early lines are exactly the pre-
    move spots the timing edge lives in. CLV is then the odds move on THIS fixed line to the close."""
    cand = [(round(float(line), 1), o) for line, (o, u) in ladder.items() if o and 1.2 <= o <= 8.0]
    if not cand:
        return None, None
    line, o = min(cand, key=lambda x: (abs(x[0] - proj), x[0]))
    return line, round(o, 3)


def main_line(ladder):
    """The book's balanced main line from {line: (over_dec, under_dec)} — the rung where the over
    and under prices are CLOSEST (both ~-110), i.e. where the book thinks the player actually lands.
    Used only as secondary context now (needs a mature ladder); the fixed-line odds are primary."""
    bal = [(round(float(line), 1), o, u) for line, (o, u) in ladder.items()
           if o and u and 1.3 <= o <= 3.5 and 1.3 <= u <= 3.5]
    if len(bal) < 2:        # thin/early ladder — the balanced rung is unreliable, don't log a shadow
        return None, None
    line, o, _u = min(bal, key=lambda x: abs(x[1] - x[2]))
    # sanity: the picked rung's over & under must BOTH be near even (a real main line, not a lone
    # mispriced deep rung that happens to read 1.9). Guard against thin-ladder artifacts.
    over, under = ladder[line] if line in ladder else (o, o)
    if not (1.55 <= over <= 2.45 and 1.55 <= under <= 2.45):
        return None, None
    return line, round(o, 3)


def log_shadow(date, player, out_player, projs, props):
    """Log the EARLIEST injury-driven flag (INSERT OR IGNORE keeps the first = the timing capture).
    projs: {stat: projection}. props: posted_props(player) = {stat: {line: (over, under)}}."""
    con = _con()
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    n = 0
    for stat, proj in projs.items():
        ladder = props.get(stat)
        if not ladder:
            continue
        fl, fo = nearest_over(ladder, proj)              # the fixed over line nearest our number
        if fl is None:
            continue
        n += con.execute(
            "INSERT OR IGNORE INTO clv(date, player, stat, out_player, flagged_at, proj, "
            "flag_line, flag_over) VALUES (?,?,?,?,?,?,?,?)",
            (date, player, stat, out_player, ts, round(proj, 1), fl, fo)).rowcount
    con.commit()
    con.close()
    return n


def _latest_ladder(con_p, player, stat, date):
    rows = con_p.execute(
        "SELECT line, side, odds, collected_at FROM fd_lines WHERE sport='wnba' AND player=? "
        "AND stat=? AND substr(collected_at,1,10) BETWEEN ? AND ?",   # ±1 day: ET slate vs UTC stamp
        (player, stat, _plus(date, -1), _plus(date, 1))).fetchall()
    best = {}
    for line, side, odds, ca in rows:
        if line is None or side not in ("over", "under"):
            continue
        k = (round(float(line), 1), side)
        if k not in best or ca > best[k][1]:
            best[k] = (float(odds), ca)
    lad = {}
    for (line, side), (odds, _ca) in best.items():
        lad.setdefault(line, [0.0, 0.0])[0 if side == "over" else 1] = odds
    return {k: tuple(v) for k, v in lad.items()}


def capture_close():
    """Fill the closing line/odds for open shadows from the LATEST logged FanDuel ladder. Run at/
    after tip so 'latest' == the closing number."""
    if not PROPS_DB.exists():
        return 0
    con = _con()
    con_p = sqlite3.connect(PROPS_DB)
    rows = con.execute("SELECT rowid, player, stat, date, flag_line FROM clv WHERE closed=0").fetchall()
    n = 0
    for rid, player, stat, date, flag_line in rows:
        lad = _latest_ladder(con_p, player, stat, date)
        if not lad:
            continue
        cl, _co = main_line(lad)                          # secondary context (needs mature ladder)
        close_over = lad.get(round(flag_line, 1), (None, None))[0]   # closing over price at OUR line
        if close_over is None and cl is not None and cl > flag_line:
            close_over = 1.10     # our over line is now well ITM (the market moved past it) = big CLV
        con.execute("UPDATE clv SET close_line=?, close_over=?, closed=1 WHERE rowid=?",
                    (cl, close_over, rid))
        n += 1
    con.commit()
    con_p.close()
    con.close()
    return n


def grade():
    con = _con()
    rows = con.execute("SELECT rowid, date, player, stat FROM clv WHERE graded=0 AND closed=1").fetchall()
    if not rows:
        con.close()
        return 0
    W.players()
    cache, n = {}, 0
    for rid, date, player, stat in rows:
        if player not in cache:
            try:
                cache[player] = W.game_log(_pid(player))
            except Exception:
                cache[player] = []
        cand = sorted((g for g in cache[player] if g.get("result") and g["date"][:10] >= date),
                      key=lambda g: g["date"])
        if not cand:
            continue
        con.execute("UPDATE clv SET actual=?, graded=1 WHERE rowid=?",
                    (cand[0][STAT_KEY[stat]], rid))
        n += 1
    con.commit()
    con.close()
    return n


def report():
    con = _con()
    con.row_factory = sqlite3.Row
    R = [dict(r) for r in con.execute(
        "SELECT * FROM clv WHERE closed=1 AND close_line IS NOT NULL AND flag_line IS NOT NULL")]
    con.close()
    # OVER spots = the over we'd actually bet (our projection sits at/above the fixed line we track)
    ov = [r for r in R if r["close_over"] and r["close_over"] > 0 and r["proj"] >= r["flag_line"] - 0.5]
    L = ["# WNBA injury-timing CLV — do our reads beat the closing number?", "",
         f"_{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · {len(R)} closed shadows "
         f"({len(ov)} over-side)_", ""]
    if len(ov) < MIN_GRADED:
        L.append(f"Accumulating — {len(ov)}/{MIN_GRADED} over shadows before CLV is trustworthy.")
        REPORT.write_text("\n".join(L) + "\n")
        print(f"clv report: {len(ov)}/{MIN_GRADED} over shadows — accumulating")
        return
    # CLV at the FIXED line: the over we locked at flag vs its price at the close. flag_over >
    # close_over  <=>  we got a LONGER price than the market closed at  <=>  positive CLV.
    clvs = [100 * (r["flag_over"] / r["close_over"] - 1) for r in ov]
    pos = sum(1 for c in clvs if c > 0)
    graded = [r for r in ov if r["graded"] and r["actual"] is not None]
    won = sum(1 for r in graded if r["actual"] > r["flag_line"])
    L += ["## Do our injury reads beat the closing number?", "```",
          f"over shadows:           {len(ov)}",
          f"avg CLV (our px vs close): {st.mean(clvs):+.1f}%   (>0 = we locked a LONGER price than "
          f"the close = we beat the book)",
          f"positive-CLV rate:      {pos}/{len(clvs)} ({100*pos/len(clvs):.0f}%)",
          (f"realized over hit:      {won}/{len(graded)} ({100*won/len(graded):.0f}%)"
           if graded else "realized over hit:      pending")]
    L += ["```",
          "Positive CLV = we consistently price the injury reprice BEFORE the book. That is the "
          "timing edge — the autobetter's green light to flag-and-notify with real money.", ""]
    REPORT.write_text("\n".join(L) + "\n")
    print(f"clv report: {len(ov)} over shadows · CLV {st.mean(clvs):+.1f}% · "
          f"{100*pos/len(clvs):.0f}% positive · wrote {REPORT.name}")


def _corr(a, b):
    if len(a) < 3 or st.pstdev(a) == 0 or st.pstdev(b) == 0:
        return 0.0
    ma, mb = st.mean(a), st.mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / len(a)
    return cov / (st.pstdev(a) * st.pstdev(b))


def _pid(player):
    import wnba_regrade as R
    return R._ids().get(player)


def _plus(date, days):
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--close", action="store_true")
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.close:
        print(f"clv close: captured {capture_close()} closing lines")
    if args.grade:
        print(f"clv grade: {grade()} newly graded")
    if args.report or not (args.close or args.grade):
        report()


if __name__ == "__main__":
    main()
