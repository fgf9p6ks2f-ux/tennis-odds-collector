"""Walks paper tracker — DK-only ('Walks Allowed O/U', collected since 2026-07-22). There is NO
validated walk edge yet; this FORWARD-captures walk UNDERS + the signals needed to test two ideas:
 (1) the transferable 'line ABOVE the pitcher's recent walks -> UNDER' pattern (it showed in outs
     AND earned runs), and
 (2) the walks-specific mechanism — a PATIENT opponent (high pitches/PA) draws more walks, so the
     under should be WORSE vs patient offenses (a signal we already compute for the outs premium).
Paper only: flags the UNDER at the FROZEN flag-time DK price, grades vs statsapi walks, stores
home / opp-patience / recent-walks so we can segment once ~2-3 weeks of DK lines accumulate. Nothing
here bets real money or feeds the board until a segment is validated forward.

    python walks_paper.py            # flag new + grade finished
    python walks_paper.py report     # print the record + segment slices
"""
import sqlite3
import datetime as dt
import statistics as st
import sys
from pathlib import Path

from mlb import data as D
import k_paper  # reuse _team_hit (opp patience by team_id) + the shared id cache

HERE = Path(__file__).resolve().parent
FD = HERE / "fanduel_props.sqlite"
DB = HERE / "walks_paper.sqlite"
WALK_MIN = 1.5                 # ignore sub-1.5 junk lines
EPOCH = "2026-07-21"           # forward OOS start = the day DK walk collection began (all bets are OOS)


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS paper (
        pitcher TEXT, game_date TEXT, book TEXT, side TEXT, line REAL, odds REAL,
        flagged_at TEXT, closed INTEGER DEFAULT 0, result TEXT, actual REAL, pnl REAL,
        graded_at TEXT, home INTEGER, opp_ppa REAL, r5 REAL,
        PRIMARY KEY (pitcher, game_date, book))""")


def _closing_walks():
    """{(pitcher, gd): (line, under_dec)} = the DK closing walk line per pitcher-day (main line, i.e.
    the rung whose under price is nearest even money — the 2-way market, not an alt rung)."""
    if not FD.exists():
        return {}
    con = sqlite3.connect(FD)
    raw = con.execute(
        "SELECT player, date(collected_at), line, side, odds, collected_at "
        "FROM fd_lines WHERE sport='mlb' AND stat='walks' AND book='dk' "
        "ORDER BY player, date(collected_at), collected_at").fetchall()
    con.close()
    byg = {}
    for pl, gd, line, side, odds, cat in raw:
        byg.setdefault((pl, gd), {}).setdefault(line, {})[side] = odds   # last snapshot wins = closing
    out = {}
    for key, lines in byg.items():
        cand = {l: v for l, v in lines.items() if "under" in v}
        if not cand:
            continue
        pick = min(cand, key=lambda l: abs((cand[l].get("over") or cand[l]["under"]) - 1.9))
        out[key] = (pick, cand[pick]["under"])
    return out


def flag():
    con = sqlite3.connect(DB)
    _ensure(con)
    ts = _now()
    added = 0
    for (pitcher, gd), (line, uo) in _closing_walks().items():
        if line is None or uo is None or line < WALK_MIN:
            continue
        seen = con.execute("SELECT 1 FROM paper WHERE pitcher=? AND game_date=? AND book='dk'",
                           (pitcher, gd)).fetchone()
        if seen is None:                          # FIRST sighting -> FREEZE the flag-time under price
            con.execute("INSERT INTO paper (pitcher,game_date,book,side,line,odds,flagged_at) "
                        "VALUES (?,?,?,?,?,?,?)", (pitcher, gd, "dk", "under", line, uo, ts))
            added += 1
    con.commit()
    con.close()
    print(f"walks flag: +{added} new")


def grade():
    con = sqlite3.connect(DB)
    _ensure(con)
    ids = k_paper._load_ids()
    today = dt.date.today().isoformat()
    todo = con.execute("SELECT DISTINCT pitcher, game_date FROM paper WHERE result IS NULL "
                       "AND game_date < ?", (today,)).fetchall()
    glcache, tkcache = {}, {}
    graded = 0
    for pitcher, gd in todo:
        pid = ids.get(pitcher)
        if pid is None and pitcher not in ids:
            pid = D.find_pitcher(pitcher)
            ids[pitcher] = pid
        if not pid:
            continue
        if pid not in glcache:
            try:
                glcache[pid] = D.pitcher_gamelog(pid, int(gd[:4]))
            except Exception:
                glcache[pid] = []
        try:
            ld = dt.date.fromisoformat(gd)
        except ValueError:
            continue
        best, g = None, None                       # tolerant ±1-day match (UTC line day vs ET game day)
        for x in glcache[pid]:
            if not x.get("date"):
                continue
            try:
                diff = abs((dt.date.fromisoformat(x["date"]) - ld).days)
            except ValueError:
                continue
            if diff <= 1 and (best is None or diff < best):
                best, g = diff, x
        if not g or g.get("bb") is None:
            continue
        season = int(gd[:4])
        if season not in tkcache:
            tkcache[season] = k_paper._team_hit(season)
        _tk, _lg, ppa_map, _p25 = tkcache[season]
        opp_ppa = ppa_map.get(g.get("opp"))
        priors = sorted([x for x in glcache[pid] if x.get("date") and x["date"] < g["date"]
                         and x.get("bb") is not None], key=lambda x: x["date"])[-5:]
        r5 = st.median(x["bb"] for x in priors) if len(priors) >= 3 else None
        for (side, line, odds) in con.execute(
                "SELECT side, line, odds FROM paper WHERE pitcher=? AND game_date=? AND result IS NULL",
                (pitcher, gd)).fetchall():
            actual = g["bb"]
            if actual == line:
                res, pnl = "push", 0.0
            else:
                won = (actual < line) if side == "under" else (actual > line)
                res, pnl = ("W", odds - 1) if won else ("L", -1.0)
            con.execute("UPDATE paper SET result=?, actual=?, pnl=?, graded_at=?, closed=1, "
                        "home=?, opp_ppa=?, r5=? WHERE pitcher=? AND game_date=? AND result IS NULL",
                        (res, actual, pnl, _now(), 1 if g.get("is_home") else 0, opp_ppa, r5,
                         pitcher, gd))
            graded += 1
    con.commit()
    con.close()
    k_paper.IDCACHE.write_text(__import__("json").dumps(ids))
    print(f"walks grade: settled {graded}")


def _slice(con, where, args=()):
    rows = con.execute(f"SELECT side, line, odds, actual, pnl FROM paper WHERE result IN ('W','L') "
                       f"AND game_date >= ? {where}", (EPOCH, *args)).fetchall()
    n = len(rows)
    w = sum(1 for r in rows if r[4] > 0)
    u = sum(r[4] for r in rows)
    return n, w, (100 * u / n if n else 0.0), u


def report():
    con = sqlite3.connect(DB)
    _ensure(con)
    pend = con.execute("SELECT COUNT(*) FROM paper WHERE result IS NULL").fetchone()[0]
    print(f"WALKS paper tracker (DK, forward from {EPOCH}) — {pend} pending\n")
    for name, where, args in (
            ("ALL walk unders", "", ()),
            ("line ABOVE recent walks", "AND r5 IS NOT NULL AND line > r5", ()),
            ("line AT/BELOW recent", "AND r5 IS NOT NULL AND line <= r5", ()),
            ("away starter", "AND home=0", ()),
            ("home starter", "AND home=1", ()),
            ("vs PATIENT offense (ppa>=3.9)", "AND opp_ppa >= 3.9", ()),
            ("vs impatient offense (ppa<3.85)", "AND opp_ppa < 3.85", ())):
        n, w, roi, u = _slice(con, where, args)
        tag = "" if n >= 15 else "   (thin)"
        print(f"  {name:34s} {w:3d}-{n-w:<3d} {100*w/n if n else 0:4.0f}%  ROI {roi:+6.1f}%  "
              f"{u:+.1f}u (n={n}){tag}")
    con.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        flag()
        grade()
        report()
