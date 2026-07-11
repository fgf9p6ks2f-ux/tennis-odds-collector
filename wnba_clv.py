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


def main_line(ladder):
    """The book's balanced main line from {line: (over_dec, under_dec)} — the rung where the over
    and under prices are CLOSEST (both ~-110), i.e. where the book thinks the player actually lands.
    (Picking 'over nearest -110' alone can grab a mispriced deep rung.)"""
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
        fl, fo = main_line(ladder)
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
        "AND stat=? AND substr(collected_at,1,10) BETWEEN ? AND ?",
        (player, stat, date, _plus(date, 1))).fetchall()
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
        cl, co = main_line(lad)
        over_at_flag = lad.get(round(flag_line, 1), (None, None))[0]   # closing over price at OUR line
        con.execute("UPDATE clv SET close_line=?, close_over=?, closed=1 WHERE rowid=?",
                    (cl, over_at_flag or co, rid))
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
    L = ["# WNBA injury-timing CLV — does our read beat the line move?", "",
         f"_{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · {len(R)} closed shadows_", ""]
    if len(R) < MIN_GRADED:
        L.append(f"Accumulating — {len(R)}/{MIN_GRADED} closed shadows before CLV is trustworthy.")
        REPORT.write_text("\n".join(L) + "\n")
        print(f"clv report: {len(R)}/{MIN_GRADED} closed — accumulating")
        return
    # our directional edge vs the realized line move
    moved_our_way = corr_num = 0
    edges, moves = [], []
    for r in R:
        edge = r["proj"] - r["flag_line"]           # + = we say line is LOW (bet over)
        move = r["close_line"] - r["flag_line"]      # + = line rose by close
        edges.append(edge)
        moves.append(move)
        if edge != 0 and (edge > 0) == (move > 0) and move != 0:
            moved_our_way += 1
    signed = [(1 if e > 0 else -1) * m for e, m in zip(edges, moves)]   # CLV in points, our direction
    # odds CLV: over got more expensive by close on the spots we liked (proj>line)
    over_spots = [r for r in R if r["proj"] > r["flag_line"] and r["flag_over"] and r["close_over"]]
    odds_clv = [100 * (r["flag_over"] / r["close_over"] - 1) for r in over_spots]
    L += ["## Did the line move the way we projected?", "```",
          f"closed shadows:        {len(R)}",
          f"line moved OUR way:    {moved_our_way}/{sum(1 for e,m in zip(edges,moves) if e and m)} "
          f"(of spots where both our edge and the line actually moved)",
          f"avg signed line-CLV:   {st.mean(signed):+.2f} pts   (>0 = the line drifts toward our read)",
          f"avg edge (proj-line):  {st.mean(edges):+.2f} pts",
          f"corr(edge, line move): {_corr(edges, moves):+.2f}   (>0 = our read predicts the move)"]
    if odds_clv:
        L += [f"over-price CLV:         {st.mean(odds_clv):+.1f}%   (>0 = our overs got more "
              f"expensive by close, n{len(odds_clv)})"]
    L += ["```",
          "If line-CLV and the correlation are positive, the injury-timing edge is REAL — we "
          "consistently price the reprice before the book. That is the autobetter's green light.", ""]
    REPORT.write_text("\n".join(L) + "\n")
    print(f"clv report: {len(R)} closed · signed line-CLV {st.mean(signed):+.2f}pts · "
          f"corr {_corr(edges, moves):+.2f} · wrote {REPORT.name}")


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
