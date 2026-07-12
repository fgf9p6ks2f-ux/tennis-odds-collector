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
MIN_DATES = 5      # distinct slates — one night's shadows are correlated, not independent evidence

SCHEMA = """CREATE TABLE IF NOT EXISTS clv(
  date TEXT, player TEXT, stat TEXT, out_player TEXT, flagged_at TEXT, proj REAL,
  flag_line REAL, flag_over REAL, close_line REAL, close_over REAL, closed INTEGER DEFAULT 0,
  actual REAL, graded INTEGER DEFAULT 0, v INTEGER, UNIQUE(date, player, stat));"""


def _con():
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    # v = format version. v2 = flag_line & close_line are BOTH the book main line (opening vs closing).
    # Pre-v2 rows mixed nearest-rung flag with main-line close (the 24.5-vs-5.5 bug) -> excluded.
    if "v" not in {r[1] for r in con.execute("PRAGMA table_info(clv)")}:
        con.execute("ALTER TABLE clv ADD COLUMN v INTEGER")
    return con


def book_line(ladder):
    """The book's MAIN line and its over price from {line: (over_dec, under_dec)} — the rung whose
    OVER price sits closest to even (~2.0 decimal), i.e. where the book thinks the player lands.
    This is the SAME concept captured at flag (the OPENING line) and at close (the CLOSING line), so
    CLV is a clean opening-vs-closing LINE move — not a mismatch of two different rungs. Works on a
    THIN early ladder (needs only one near-even rung, which is exactly the pre-move spot the timing
    edge lives in); returns (None, None) if the ladder is too skewed to identify a main line (only
    deep alt rungs posted, no rung near even)."""
    cand = [(round(float(line), 1), o) for line, (o, u) in ladder.items() if o and 1.3 <= o <= 3.5]
    if not cand:
        return None, None
    line, o = min(cand, key=lambda x: abs(x[1] - 2.0))
    if not (1.6 <= o <= 2.6):            # closest rung STILL isn't near even -> no real main line here
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
        fl, fo = book_line(ladder)                       # the book's MAIN line NOW = our OPENING line
        if fl is None:
            continue
        n += con.execute(
            "INSERT OR IGNORE INTO clv(date, player, stat, out_player, flagged_at, proj, "
            "flag_line, flag_over, v) VALUES (?,?,?,?,?,?,?,?,2)",
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
    rows = con.execute("SELECT rowid, player, stat, date FROM clv WHERE closed=0 AND v=2").fetchall()
    n = 0
    for rid, player, stat, date in rows:
        lad = _latest_ladder(con_p, player, stat, date)
        if not lad:
            continue
        cl, co = book_line(lad)                           # the CLOSING main line — same concept as flag
        if cl is None:                                    # closing ladder too thin -> leave open, retry
            continue
        con.execute("UPDATE clv SET close_line=?, close_over=?, closed=1 WHERE rowid=?",
                    (cl, co, rid))
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


def verdict():
    """Structured CLV verdict, shared by report() (markdown) and the dashboard (panel). LINE CLV:
    the SAME book main line at the open (flag) vs the close, signed TOWARD our read — an over read
    (proj above the opening line) wins when the line RISES, an under read when it FALLS. Returns
    {n, need, ready, line_move (pts, signed toward us), pos_rate, corr, hit, hit_n}."""
    con = _con()
    con.row_factory = sqlite3.Row
    R = [dict(r) for r in con.execute(
        "SELECT * FROM clv WHERE v=2 AND closed=1 AND close_line IS NOT NULL AND flag_line IS NOT NULL")]
    con.close()
    n = len(R)
    dates = len({r["date"] for r in R})
    # READY needs BOTH volume and breadth: one slate's shadows are CORRELATED (same games, same
    # injuries) — 24 shadows from one night is one observation, not 24. No verdict until the sample
    # spans enough distinct slates to mean something.
    base = {"n": n, "need": MIN_GRADED, "dates": dates, "need_dates": MIN_DATES,
            "ready": n >= MIN_GRADED and dates >= MIN_DATES,
            "line_move": None, "pos_rate": None, "corr": None, "hit": None, "hit_n": 0}
    if not n:
        return base
    moves = [(r["close_line"] - r["flag_line"]) * (1 if r["proj"] >= r["flag_line"] else -1) for r in R]
    graded = [r for r in R if r["graded"] and r["actual"] is not None]
    won = sum(1 for r in graded if (r["actual"] > r["flag_line"]) == (r["proj"] >= r["flag_line"]))
    base.update(line_move=st.mean(moves), pos_rate=sum(1 for m in moves if m > 0) / n,
                corr=_corr([r["proj"] - r["flag_line"] for r in R],
                           [r["close_line"] - r["flag_line"] for r in R]),
                hit=(won / len(graded) if graded else None), hit_n=len(graded))
    return base


def report():
    v = verdict()
    L = ["# WNBA injury-timing CLV — does the line move our way, open to close?", "",
         f"_{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · {v['n']} closed shadows "
         f"(opening line vs closing line)_", ""]
    if not v["ready"]:
        L.append(f"Accumulating — {v['n']}/{v['need']} closed shadows over {v['dates']}/{v['need_dates']} "
                 f"slates before CLV is trustworthy (one slate's shadows are correlated).")
        REPORT.write_text("\n".join(L) + "\n")
        print(f"clv report: {v['n']}/{v['need']} shadows · {v['dates']}/{v['need_dates']} slates — accumulating")
        return
    L += ["## Does the line move toward our read from open to close?", "```",
          f"closed shadows:            {v['n']}",
          f"avg line move toward us:   {v['line_move']:+.2f} pts   (>0 = the close moved our way)",
          f"positive-CLV rate:         {100*v['pos_rate']:.0f}%",
          f"corr(our edge, line move): {v['corr']:+.2f}   (does proj-minus-open predict open-to-close?)",
          (f"realized hit (our side):   {100*v['hit']:.0f}% ({v['hit_n']})" if v["hit"] is not None
           else "realized hit (our side):   pending")]
    L += ["```",
          "The line moving toward our read between the flag (open) and the close = we price the "
          "injury reprice BEFORE the book. That is the timing edge — the green light to bet real money.",
          ""]
    REPORT.write_text("\n".join(L) + "\n")
    print(f"clv report: {v['n']} closed shadows · line move {v['line_move']:+.2f} · "
          f"{100*v['pos_rate']:.0f}% positive · wrote {REPORT.name}")


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
