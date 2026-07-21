"""Forward paper-tracker for the two candidate MLB pitcher-prop edges (2026-07-21).

These are UNCONFIRMED leads found in a 2-week backtest that beat Pinnacle + held across
split-halves — but 2 weeks is too thin to bet real money. This banks the OUT-OF-SAMPLE
evidence forward so we can confirm or kill them honestly (memory: real-lines-only,
validate-before-shipping, no-MAE — record/hit%/ROI only).

  RULE 1  k_over    : bet OVER when a pitcher's STRIKEOUT line is >= 6.5
                      (books anchor high-K arms conservatively; overs underpriced)
  RULE 2  outs_under: bet UNDER when a pitcher's PITCHING-OUTS line is <= 16.5
                      (league-wide starter-length decline; books price outs a touch long)

Each qualifying pitcher-game logs a paper bet at BOTH books' closing line/odds where
available: Pinnacle (mlb_kprops.sqlite — the hard benchmark) and FanDuel (fanduel_props
.sqlite — the real target, softer). Graded vs actual K / outs from statsapi gamelogs.

    python k_paper.py            # flag new + update-to-close + grade finished + report
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mlb import data  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = HERE / "k_paper.sqlite"
PINN = HERE / "mlb_kprops.sqlite"
FD = HERE / "fanduel_props.sqlite"
IDCACHE = HERE / "k_paper_ids.json"

K_OVER_MIN = 6.5        # RULE 1: strikeout line >= this -> OVER
OUTS_UNDER_MAX = 16.5   # RULE 2: outs line <= this -> UNDER
EPOCH = "2026-07-22"    # games on/after this = the true FORWARD (out-of-sample) test;
#                         earlier games are the in-sample seed (the backtest), shown separately

DDL = """CREATE TABLE IF NOT EXISTS paper (
  pitcher TEXT, game_date TEXT, market TEXT, rule TEXT, book TEXT,
  side TEXT, line REAL, odds REAL, flagged_at TEXT, closed INTEGER DEFAULT 0,
  result TEXT, actual INTEGER, pnl REAL, graded_at TEXT, home INTEGER, opp_k REAL,
  PRIMARY KEY (pitcher, game_date, market, book))"""


CONTACT_MAX = 0.225     # opponent team K% below this = a CONTACT offense (balls in play ->
#                         traffic -> higher pitch count -> earlier hook -> outs-under stacks)


def _ensure(con):
    """Create the table + add later columns to an existing DB (2026-07-21: the outs-under edge
    is really an AWAY-starter effect — away go ~0.43 outs shorter, t≈2.8/2248 starts — and it
    STACKS with contact offenses, so we tag home/away + opponent K% and validate forward)."""
    con.execute(DDL)
    cols = {r[1] for r in con.execute("PRAGMA table_info(paper)")}
    if "home" not in cols:
        con.execute("ALTER TABLE paper ADD COLUMN home INTEGER")
    if "opp_k" not in cols:
        con.execute("ALTER TABLE paper ADD COLUMN opp_k REAL")


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def _load_ids():
    if IDCACHE.exists():
        try:
            return json.loads(IDCACHE.read_text())
        except ValueError:
            pass
    return {}


def _closing_lines(db, market_stat):
    """{(pitcher, game_date): (line, over_dec, under_dec, start_iso)} = latest snapshot
    at or before first pitch, for a given prop stat, from a *_props.sqlite line store."""
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    # both stores share (pitcher/player, stat, line, over_odds/under_odds, start_time-ish)
    if db == PINN:
        q = ("SELECT pitcher, date(start_time), line, over_odds, under_odds, start_time, collected_at "
             "FROM pitcher_props WHERE stat=? AND start_time IS NOT NULL AND collected_at<=start_time "
             "ORDER BY pitcher, date(start_time), collected_at")
        rows = con.execute(q, (market_stat,)).fetchall()
    else:  # FanDuel fd_lines: no start_time; use the event day = collected day (props post game-day)
        q = ("SELECT player, date(collected_at), line, side, odds, collected_at "
             "FROM fd_lines WHERE sport='mlb' AND stat=? ORDER BY player, date(collected_at), collected_at")
        raw = con.execute(q, (market_stat,)).fetchall()
        con.close()
        # FD ladder is one-sided rows; fold to (line -> {over,under}) per pitcher-day, take the MAIN
        # line (closest to a fair ~even market) — approximate: the line whose over odds are nearest 1.9
        byg = {}
        for pl, gd, line, side, odds, cat in raw:
            byg.setdefault((pl, gd), {}).setdefault(line, {})[side] = (odds, cat)
        out = {}
        for key, lines in byg.items():
            two = {l: v for l, v in lines.items() if "over" in v and "under" in v}
            pick = None
            if two:
                pick = min(two, key=lambda l: abs(two[l]["over"][0] - 1.9))
                oo, uo = two[pick]["over"][0], two[pick]["under"][0]
            else:  # only over ladder — take the rung nearest even money as the "main"
                pick = min(lines, key=lambda l: abs(lines[l].get("over", (99,))[0] - 1.9))
                oo = lines[pick].get("over", (None,))[0]
                uo = lines[pick].get("under", (None,))[0]
            out[key] = (pick, oo, uo, None)
        return out
    con.close()
    last = {}
    for pitcher, gd, line, oo, uo, start, cat in rows:
        if oo and uo:
            last[(pitcher, gd)] = (line, oo, uo, start)
    return last


def _qualifies(market, line):
    if market == "k" and line is not None and line >= K_OVER_MIN:
        return "over", "k_over"
    if market == "outs" and line is not None and line <= OUTS_UNDER_MAX:
        return "under", "outs_under"
    return None, None


def flag():
    con = sqlite3.connect(DB)
    _ensure(con)
    ts = _now()
    added = updated = 0
    for market, stat, books in (("k", "strikeouts", ((PINN, "pinn"), (FD, "fd"))),
                                ("outs", "outs", ((PINN, "pinn"), (FD, "fd")))):
        for db, book in books:
            for (pitcher, gd), (line, oo, uo, start) in _closing_lines(db, stat).items():
                side, rule = _qualifies(market, line)
                if not side:
                    continue
                odds = oo if side == "over" else uo
                if odds is None:
                    continue
                row = con.execute("SELECT closed FROM paper WHERE pitcher=? AND game_date=? "
                                  "AND market=? AND book=?", (pitcher, gd, market, book)).fetchone()
                if row is None:
                    con.execute("INSERT INTO paper (pitcher,game_date,market,rule,book,side,line,"
                                "odds,flagged_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (pitcher, gd, market, rule, book, side, line, odds, ts))
                    added += 1
                elif not row[0]:                       # not yet closed -> refresh toward closing line
                    con.execute("UPDATE paper SET line=?, odds=? WHERE pitcher=? AND game_date=? "
                                "AND market=? AND book=?", (line, odds, pitcher, gd, market, book))
                    updated += 1
    con.commit()
    con.close()
    print(f"flag: +{added} new, {updated} refreshed")


def grade():
    con = sqlite3.connect(DB)
    _ensure(con)
    ids = _load_ids()
    today = dt.date.today().isoformat()
    todo = con.execute("SELECT DISTINCT pitcher, game_date FROM paper WHERE result IS NULL "
                       "AND game_date < ?", (today,)).fetchall()
    logcache = {}
    tkcache = {}                                          # season -> (team K% map, league K%)
    graded = 0
    for pitcher, gd in todo:
        pid = ids.get(pitcher)
        if pid is None and pitcher not in ids:
            pid = data.find_pitcher(pitcher)
            ids[pitcher] = pid
        if not pid:
            continue
        if pid not in logcache:
            season = int(gd[:4])
            try:
                logcache[pid] = data.pitcher_gamelog(pid, season)
            except Exception:
                logcache[pid] = []
        g = next((x for x in logcache[pid] if x["date"] == gd and x["bf"] >= 5), None)
        if not g:                                       # scratched / not final yet
            continue
        season = int(gd[:4])
        if season not in tkcache:
            try:
                tkcache[season] = data.team_kpct(season)
            except Exception:
                tkcache[season] = ({}, 0.22)
        tk, lg_k = tkcache[season]
        opp_k = tk.get(g.get("opp_id"), lg_k)
        for market, keyk in (("k", "k"), ("outs", "outs")):
            for (side, line, odds) in con.execute(
                    "SELECT side, line, odds FROM paper WHERE pitcher=? AND game_date=? AND market=? "
                    "AND result IS NULL", (pitcher, gd, market)).fetchall():
                actual = g[keyk]
                if actual == line:
                    res, pnl = "push", 0.0
                else:
                    won = (actual > line) if side == "over" else (actual < line)
                    res, pnl = ("W", odds - 1) if won else ("L", -1.0)
                home = 1 if g.get("is_home") else 0
                con.execute("UPDATE paper SET result=?, actual=?, pnl=?, graded_at=?, closed=1, "
                            "home=?, opp_k=? WHERE pitcher=? AND game_date=? AND market=? "
                            "AND result IS NULL",
                            (res, actual, pnl, _now(), home, opp_k, pitcher, gd, market))
                graded += 1
    con.commit()
    con.close()
    IDCACHE.write_text(json.dumps(ids))
    print(f"grade: settled {graded}")


def _bucket(con, rule, book, where, args):
    g = con.execute(f"SELECT COUNT(*), SUM(result='W'), SUM(result='L'), COALESCE(SUM(pnl),0) "
                    f"FROM paper WHERE rule=? AND book=? AND result IN ('W','L'){where}",
                    (rule, book, *args)).fetchone()
    n, w, l, pnl = g[0], g[1] or 0, g[2] or 0, g[3] or 0
    return n, w, l, pnl


def report():
    con = sqlite3.connect(DB)
    _ensure(con)
    for label, where, args in [("FORWARD (out-of-sample, the real test)", " AND game_date>=?", (EPOCH,)),
                               ("in-sample seed (the 2-wk backtest, for reference)", " AND game_date<?", (EPOCH,))]:
        print(f"\n=== MLB pitcher-prop PAPER edges — {label} ===")
        for rule, book in [("k_over", "pinn"), ("k_over", "fd"),
                           ("outs_under", "pinn"), ("outs_under", "fd")]:
            n, w, l, pnl = _bucket(con, rule, book, where, args)
            openn = con.execute(f"SELECT COUNT(*) FROM paper WHERE rule=? AND book=? AND result IS NULL"
                                f"{where}", (rule, book, *args)).fetchone()[0]
            roi = pnl / n * 100 if n else 0
            hit = w / n * 100 if n else 0
            print(f"  {rule:11} @ {book:4}  {w}-{l}  ({hit:.0f}%)  {pnl:+.2f}u  ROI {roi:+.1f}%   [{openn} open]")
    # per-line slice for outs (is 15.5 really the sweet spot going forward?)
    print("\n  outs_under by line (pinn, all):", end=" ")
    for line, n, w, pnl in con.execute(
            "SELECT line, COUNT(*), SUM(result='W'), COALESCE(SUM(pnl),0) FROM paper "
            "WHERE rule='outs_under' AND book='pinn' AND result IN ('W','L') GROUP BY line ORDER BY line"):
        print(f"{line}:{w}/{n}({pnl:+.1f}u)", end="  ")
    # ★ the DIAMOND: outs-under is really an AWAY-starter effect (away go ~0.43 outs shorter, t≈2.8).
    # Track the home/away split forward — away should keep winning, home should keep losing.
    print("\n  outs_under home/away (pinn, all):", end=" ")
    for lbl, hv in (("AWAY", 0), ("HOME", 1)):
        g = con.execute("SELECT COUNT(*), SUM(result='W'), COALESCE(SUM(pnl),0) FROM paper "
                        "WHERE rule='outs_under' AND book='pinn' AND result IN ('W','L') AND home=?",
                        (hv,)).fetchone()
        n, w, pnl = g[0], g[1] or 0, g[2] or 0
        roi = pnl / n * 100 if n else 0
        print(f"{lbl} {w}/{n} ({roi:+.0f}%)", end="   ")
    # ★ stacked: AWAY + CONTACT offense (opp K% < CONTACT_MAX) — the sharpest slice
    print("\n  outs_under AWAY x offense (pinn):", end=" ")
    for lbl, cond in (("AWAY+CONTACT", f"opp_k < {CONTACT_MAX}"), ("AWAY+whiff", f"opp_k >= {CONTACT_MAX}")):
        g = con.execute(f"SELECT COUNT(*), SUM(result='W'), COALESCE(SUM(pnl),0) FROM paper "
                        f"WHERE rule='outs_under' AND book='pinn' AND result IN ('W','L') "
                        f"AND home=0 AND opp_k IS NOT NULL AND {cond}").fetchone()
        n, w, pnl = g[0], g[1] or 0, g[2] or 0
        roi = pnl / n * 100 if n else 0
        print(f"{lbl} {w}/{n} ({roi:+.0f}%)", end="   ")
    print()
    con.close()


if __name__ == "__main__":
    flag()
    grade()
    report()
