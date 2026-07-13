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

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = dt.timezone(dt.timedelta(hours=-4))

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_clv.sqlite"
REPORT = HERE / "wnba_clv.md"
PROPS_DB = HERE / "fanduel_props.sqlite"
STAT_KEY = {"points": "pts", "rebounds": "reb", "assists": "ast"}
MIN_GRADED = 20
MIN_DATES = 5      # distinct slates — one night's shadows are correlated, not independent evidence
FRESH_MIN = 90     # only trust ladder rungs re-posted within this many min of the newest in-window
                   # stamp — same rule posted_props uses, so a stale/phantom rung can't pose as a line

SCHEMA = """CREATE TABLE IF NOT EXISTS clv(
  date TEXT, player TEXT, stat TEXT, out_player TEXT, flagged_at TEXT, proj REAL,
  flag_line REAL, flag_over REAL, close_line REAL, close_over REAL, closed INTEGER DEFAULT 0,
  actual REAL, graded INTEGER DEFAULT 0, v INTEGER, UNIQUE(date, player, stat));"""


def _con():
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    # v = format version. v2 = flag_line & close_line are BOTH the book main line (opening vs closing).
    # Pre-v2 rows mixed nearest-rung flag with main-line close (the 24.5-vs-5.5 bug) -> excluded.
    cols = {r[1] for r in con.execute("PRAGMA table_info(clv)")}
    if "v" not in cols:
        con.execute("ALTER TABLE clv ADD COLUMN v INTEGER")
    if "tip" not in cols:                    # naive-UTC game tip time -> when the 'close' is real
        con.execute("ALTER TABLE clv ADD COLUMN tip TEXT")
    if "tier" not in cols:                   # 'firm' (validated n>=2) vs 'n1_speed' (first-occurrence
        con.execute("ALTER TABLE clv ADD COLUMN tier TEXT DEFAULT 'firm'")  # pilot) — split the CLV
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


def log_shadow(date, player, out_player, projs, props, tip=None, tier="firm"):
    """Log the EARLIEST injury-driven flag (INSERT OR IGNORE keeps the first = the timing capture).
    projs: {stat: projection}. props: posted_props(player) = {stat: {line: (over, under)}}. tip = the
    game's naive-UTC tip datetime (or its iso) so the close is captured pre-tip, not seconds later.
    tier: 'firm' (the validated n>=2 plays) or 'n1_speed' (the first-occurrence pilot) — so the report
    can judge the pilot's CLV separately without mixing it into the firm number."""
    con = _con()
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    tip_iso = tip.isoformat() if hasattr(tip, "isoformat") else (tip or None)
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
            "flag_line, flag_over, v, tip, tier) VALUES (?,?,?,?,?,?,?,?,2,?,?)",
            (date, player, stat, out_player, ts, round(proj, 1), fl, fo, tip_iso, tier)).rowcount
    con.commit()
    con.close()
    return n


def _latest_ladder(con_p, player, stat, date, before=None):
    """The player's CURRENT ladder for the slate on `date`: latest price per (line,side), but ONLY
    from the newest snapshot — rungs whose newest stamp is >FRESH_MIN older than the newest in-window
    stamp are dropped (the same rule posted_props uses, so flag and close share one laddering
    definition; without it a stale/phantom rung from a prior collection posed as the closing line and
    fabricated huge fake CLV moves). `before` (a tip iso) caps to pre-tip lines so the NEXT game's
    lines can't leak in as this game's close."""
    rows = con_p.execute(
        "SELECT line, side, odds, collected_at FROM fd_lines WHERE sport='wnba' AND player=? "
        "AND stat=? AND substr(collected_at,1,10) BETWEEN ? AND ?",   # ±1 day: ET slate vs UTC stamp
        (player, stat, _plus(date, -1), _plus(date, 1))).fetchall()
    rows = [(l, s, o, ca) for (l, s, o, ca) in rows
            if l is not None and s in ("over", "under") and (before is None or ca <= before)]
    if not rows:
        return {}
    newest = max(ca for _, _, _, ca in rows)             # the current (pre-tip if capped) snapshot stamp
    try:
        cutoff = (dt.datetime.fromisoformat(newest) - dt.timedelta(minutes=FRESH_MIN)).isoformat()
    except ValueError:
        cutoff = ""
    best = {}
    for line, side, odds, ca in rows:
        if ca < cutoff:                                  # a rung not re-posted this snapshot -> stale, drop
            continue
        k = (round(float(line), 1), side)
        if k not in best or ca > best[k][1]:             # latest price for the rung (never max-over-time)
            best[k] = (float(odds), ca)
    lad = {}
    for (line, side), (odds, _ca) in best.items():
        lad.setdefault(line, [0.0, 0.0])[0 if side == "over" else 1] = odds
    return {k: tuple(v) for k, v in lad.items()}


def capture_close():
    """ROLL each open shadow's close toward the latest PRE-TIP main line every pass, and FREEZE
    (closed=1) only once the game has tipped — so close_line is the real closing number, not the line
    seconds after the flag. (The old one-shot 'first book_line after flag' collapsed the open->close
    interval to ~0 because --close runs every ~10 min: 89% of shadows showed zero move, so the timing
    edge — the whole thesis — was never measured.)"""
    import wnba_props_db as PDB
    db = PDB.props_db()                                # freshest lines DB (resilient to a dropped cron)
    if not Path(db).exists():
        return 0
    con = _con()
    con_p = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT rowid, player, stat, date, tip FROM clv WHERE closed=0 AND v=2").fetchall()
    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    today_et = dt.datetime.now(ET).date().isoformat()
    n = 0
    for rid, player, stat, date, tip in rows:
        # cap the ladder to PRE-TIP lines so the next game's lines can't become this close: exact when
        # tip is known, else the end of the ET slate's UTC-next-day window (legacy rows logged pre-fix).
        before = tip if tip else (_plus(date, 1) + "T23:59:59")
        lad = _latest_ladder(con_p, player, stat, date, before=before)
        if lad:
            cl, co = book_line(lad)
            if cl is not None:                            # roll close toward the latest pre-tip main line
                con.execute("UPDATE clv SET close_line=?, close_over=? WHERE rowid=?", (cl, co, rid))
        # freeze only once the game has tipped (known tip past, or a prior slate for legacy rows), and
        # only if we actually captured a close_line — else leave open and retry next pass.
        tipped = (tip and now_utc >= tip) or (not tip and date < today_et)
        if tipped and con.execute("SELECT close_line FROM clv WHERE rowid=?", (rid,)).fetchone()[0] is not None:
            con.execute("UPDATE clv SET closed=1 WHERE rowid=?", (rid,))
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


def verdict(tier="firm"):
    """Structured CLV verdict, shared by report() (markdown) and the dashboard (panel). LINE CLV:
    the SAME book main line at the open (flag) vs the close, signed TOWARD our read — an over read
    (proj above the opening line) wins when the line RISES, an under read when it FALLS. Returns
    {n, need, ready, line_move (pts, signed toward us), pos_rate, corr, hit, hit_n}. `tier` keeps the
    firm plays and the n1_speed pilot SEPARATE, so the pilot never contaminates the firm CLV number
    (pre-tier rows have NULL tier, counted as 'firm')."""
    con = _con()
    con.row_factory = sqlite3.Row
    R = [dict(r) for r in con.execute(
        "SELECT * FROM clv WHERE v=2 AND closed=1 AND close_line IS NOT NULL AND flag_line IS NOT NULL "
        "AND COALESCE(tier,'firm')=?", (tier,))]
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
    p = verdict("n1_speed")                                  # the first-occurrence pilot, kept separate
    def _pilot():
        if not p["n"]:
            return []
        b = ["### ⚡ n1 speed-tier pilot — EXPERIMENTAL, separate from the firm number above", "```",
             f"closed pilot shadows:      {p['n']} over {p['dates']} slate(s)"]
        if p["line_move"] is not None:
            b += [f"avg line move toward us:   {p['line_move']:+.2f} pts",
                  f"positive-CLV rate:         {100*p['pos_rate']:.0f}%"]
        return b + ["```", "First-occurrence (1-game-sample) plays, flagged only on a STALE line. Judge "
                    "the pilot HERE before trusting it — positive pilot CLV = the speed thesis holds for "
                    "thin samples too.", ""]
    if not v["ready"]:
        L.append(f"Accumulating — {v['n']}/{v['need']} closed shadows over {v['dates']}/{v['need_dates']} "
                 f"slates before CLV is trustworthy (one slate's shadows are correlated).")
        L += [""] + _pilot()
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
    L += _pilot()
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
