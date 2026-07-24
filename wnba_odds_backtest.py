#!/usr/bin/env python3
"""Backtest the WNBA injury→beneficiary→OVER model on a REAL FD/DK historical-odds archive from
The Odds API. Sibling of mlb_odds_backtest.py, but the WNBA 'bet' is defined by an INJURY EVENT, not
by the odds — so this doesn't reinvent the model, it EXTENDS wnba_backtest.py's proven leak-free
replay (detect who sat → project beneficiaries from prior games only → look up the actually-posted
line → grade vs the box score) from its 2-day window (all we've collected live) to 3 seasons of real
FD/DK props (The Odds API player-props archive, 5-min snapshots back to 2023-05-03).

⚠ WHY WNBA IS ONLY PARTIALLY BACKTESTABLE (unlike MLB): the live edge is SPEED — bet the beneficiary's
over in the ~1-2 min window BEFORE the book reprices the injury news. A snapshot archive can't
reproduce that fill. So we BRACKET it instead of pretending to pin it:
  • opener  — the earliest snapshot (set before the injury was known) = OPTIMISTIC pre-news price.
  • tip     — the latest pre-tip snapshot (news mostly absorbed)      = CONSERVATIVE post-reprice price.
The live ledger (+9.4% overs) sits between. So trust this for STRUCTURAL questions that don't depend on
fill timing — overs-only vs unders, WHICH STATS carry (the reb-component worry), depth cap, ladder
selection — and read the absolute ROI as a [tip, opener] band, not a point estimate. [[feedback_speed_is_the_edge]]

Stages mirror the MLB harness:
  estimate  — OFFLINE credit math (WNBA props are ~market-count× costlier than MLB — see below).
  fetch     — PAID. Pull opener+tip WNBA player-prop snapshots into wnba_odds_hist.sqlite (fd_lines
              schema + a snap_kind column), resumable, quota-logged.
  backtest  — OFFLINE. Replay wnba_backtest.run() over every archived date, once per bracket; report
              overs-only, by stat, opener-vs-tip band.
  self-test — OFFLINE, no key. Parser + archive round-trip into the exact historical_props() shape.

Usage:
  export THE_ODDS_API_KEY=...
  python3 wnba_odds_backtest.py self-test
  python3 wnba_odds_backtest.py estimate --start 2025-05-01 --end 2025-09-30
  python3 wnba_odds_backtest.py fetch    --start 2025-05-01 --end 2025-09-30
  python3 wnba_odds_backtest.py backtest
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_odds_hist.sqlite"
API = "https://api.the-odds-api.com/v4"
SPORT = "basketball_wnba"
KEY_ENV = "THE_ODDS_API_KEY"
BOOKS = ("fanduel", "draftkings")

# The Odds API market key -> the model's stat name (wnba_tonight.PROP_STATS). Default fetch = the 4
# core markets the ladder/parlay logic leans on; --all-markets adds the 3 remaining combos. Cost
# scales linearly with market count, so the default keeps a season affordable.
MARKET_STAT = {
    "player_points": "points", "player_rebounds": "rebounds", "player_assists": "assists",
    "player_points_rebounds_assists": "pra",
    "player_points_rebounds": "pts_reb", "player_points_assists": "pts_ast",
    "player_rebounds_assists": "reb_ast",
}
CORE_MARKETS = ["player_points", "player_rebounds", "player_assists", "player_points_rebounds_assists"]
STAT_MARKET = {v: k for k, v in MARKET_STAT.items()}

CREDIT_PER_MARKET_EVENT = 10       # additional-market cost: 10 per region per market per event
CREDIT_PER_EVENTS_LIST = 1


def _key() -> str:
    k = os.environ.get(KEY_ENV)
    if not k:
        sys.exit(f"set {KEY_ENV} (from the-odds-api.com). `self-test`/`estimate` need no key.")
    return k


def _get(path: str, **params):
    import requests
    params["apiKey"] = _key()
    r = requests.get(f"{API}{path}", params=params, timeout=40)
    rem, used = r.headers.get("x-requests-remaining"), r.headers.get("x-requests-used")
    if r.status_code == 422:
        return None, rem, used
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code} {r.url.split('apiKey=')[0]} :: {r.text[:200]}")
    return r.json(), rem, used


# ── storage: fd_lines-compatible so wnba_backtest.historical_props reads it unchanged ────────────
def _con() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS fd_lines(
        sport TEXT, event TEXT, player TEXT, stat TEXT, line REAL, side TEXT, odds REAL,
        book TEXT, collected_at TEXT, snap_kind TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_fdl ON fd_lines(sport,player,collected_at)")
    c.execute("CREATE TABLE IF NOT EXISTS fetch_log(kind TEXT, ref TEXT, ts TEXT, PRIMARY KEY(kind,ref))")
    return c


def _logged(c, kind, ref) -> bool:
    return c.execute("SELECT 1 FROM fetch_log WHERE kind=? AND ref=?", (kind, ref)).fetchone() is not None


def _mark(c, kind, ref):
    c.execute("INSERT OR REPLACE INTO fetch_log VALUES(?,?,?)",
              (kind, ref, dt.datetime.utcnow().isoformat(timespec="seconds")))


# ── parsing (pure — the testable core) ─────────────────────────────────────────────────────────
def parse_event_odds(data: dict, game_date: str, snap_kind: str) -> list[dict]:
    """Flatten one historical event-odds `data` blob into fd_lines rows for FD/DK WNBA player props.
    collected_at is set to the ET game date so historical_props' substr(collected_at,1,10)=date match
    finds it; snap_kind tags the bracket (opener/tip)."""
    rows = []
    ev = (data or {})
    label = f'{ev.get("away_team","?")} @ {ev.get("home_team","?")}'
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    for bk in ev.get("bookmakers") or []:
        if bk.get("key") not in BOOKS:
            continue
        for mk in bk.get("markets") or []:
            stat = MARKET_STAT.get(mk.get("key"))
            if not stat:
                continue
            for o in mk.get("outcomes") or []:
                side = (o.get("name") or "").lower()
                if side not in ("over", "under") or o.get("point") is None or o.get("price") is None:
                    continue
                rows.append({"sport": "wnba", "event": label, "player": o.get("description"),
                             "stat": stat, "line": float(o["point"]), "side": side,
                             "odds": float(o["price"]), "book": bk["key"],
                             # collected_at date = game date (what historical_props keys on); keep a
                             # real clock time so ordering within the day is sane.
                             "collected_at": f"{game_date}T12:00:00" if snap_kind == "opener"
                             else f"{game_date}T22:00:00", "snap_kind": snap_kind,
                             "_now": now})
    return rows


def _drange(start, end):
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    while d0 <= d1:
        yield d0
        d0 += dt.timedelta(days=1)


def _in_season(d: dt.date) -> bool:
    return 5 <= d.month <= 10                 # WNBA regular season + playoffs (May–Oct)


# ── estimate (offline) ─────────────────────────────────────────────────────────────────────────
def cmd_estimate(a):
    markets = MARKET_STAT.keys() if a.all_markets else CORE_MARKETS
    nmk = len(list(markets))
    snaps = 2 if a.opener else 1
    days = [d for d in _drange(a.start, a.end) if _in_season(d)]
    events = a.games_per_day * len(days)
    credits = events * nmk * snaps * CREDIT_PER_MARKET_EVENT + len(days) * CREDIT_PER_EVENTS_LIST
    print(f"range {a.start} → {a.end}: {len(days)} in-season days, ~{events} events "
          f"(@ {a.games_per_day}/day)")
    print(f"markets: {nmk} ({'all 7' if a.all_markets else 'core 4'})   snapshots/event: {snaps} "
          f"({'opener+tip' if a.opener else 'tip only'})")
    print(f"estimated credits: {credits:,}")
    print(f"   = {events:,} events × {nmk} markets × {snaps} snaps × {CREDIT_PER_MARKET_EVENT}"
          f" + {len(days)} list calls")
    print("\nWNBA props cost ~market-count× more than MLB. Levers if it overruns one month's quota:")
    print("  • drop --opener (tip-only halves it; tip is the conservative/defensible number)")
    print("  • core 4 markets, not all 7   • one season at a time (archive is resumable)")
    print("⚠ verify the per-market credit cost against current the-odds-api.com docs first.")


# ── fetch (paid) ───────────────────────────────────────────────────────────────────────────────
def cmd_fetch(a):
    c = _con()
    markets = list(MARKET_STAT.keys()) if a.all_markets else CORE_MARKETS
    mk_param = ",".join(markets)
    total = 0
    for d in _drange(a.start, a.end):
        if not _in_season(d):
            continue
        dref = d.isoformat()
        if _logged(c, "day", dref) and not a.refresh:
            continue
        ev_json, rem, used = _get(f"/historical/sports/{SPORT}/events", date=f"{dref}T18:00:00Z")
        events = ((ev_json or {}).get("data")) or []
        print(f"{dref}: {len(events)} events  [quota remaining {rem}]", flush=True)
        for ev in events:
            eid, ct = ev.get("id"), ev.get("commence_time")
            if not eid:
                continue
            snaps = [("tip", 45)] + ([("opener", None)] if a.opener else [])
            for kind, lead in snaps:
                if _logged(c, f"ev_{kind}", eid):
                    continue
                when = ct
                if lead is not None:
                    try:
                        when = (dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                                - dt.timedelta(minutes=lead)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    except Exception:
                        pass
                else:
                    # opener: ask for a snapshot ~2 days before tip; the API returns the nearest
                    # available, which is effectively the opener for that event.
                    try:
                        when = (dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                                - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    except Exception:
                        pass
                od_json, rem, used = _get(f"/historical/sports/{SPORT}/events/{eid}/odds",
                                          date=when, regions="us", markets=mk_param,
                                          oddsFormat="decimal", bookmakers=",".join(BOOKS))
                rows = parse_event_odds((od_json or {}).get("data") or {}, dref, kind)
                for r in rows:
                    c.execute("INSERT INTO fd_lines VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (r["sport"], r["event"], r["player"], r["stat"], r["line"],
                               r["side"], r["odds"], r["book"], r["collected_at"], r["snap_kind"]))
                total += len(rows)
                _mark(c, f"ev_{kind}", eid)
                c.commit()
                time.sleep(a.sleep)
        _mark(c, "day", dref)
        c.commit()
    print(f"done. {total} new rows in {DB.name}")


# ── backtest (offline) — replay the proven chain over the archive, once per bracket ──────────────
def _tally(rs, label):
    if not rs:
        print(f"  {label:26s} (none)")
        return
    w = sum(1 for r in rs if r[8])
    u = sum((r[5] - 1) if r[8] else -1 for r in rs)
    print(f"  {label:26s} {w}-{len(rs)-w}  {u:+6.1f}u  win {w/len(rs)*100:3.0f}%  ROI {u/len(rs)*100:+.0f}%")


def _dedup(g):
    """One bet per (day, player, stat, line), highest-EV attribution — matches wnba_backtest.main."""
    seen, spots = set(), []
    for r in sorted(g, key=lambda r: -r[6]):
        k = (r[0], r[2], r[3], r[4])
        if k not in seen:
            seen.add(k)
            spots.append(r)
    return spots


def cmd_backtest(a):
    import wnba_backtest as WB
    if not DB.exists():
        sys.exit(f"{DB.name} missing — run `fetch` first (or `self-test` to check the logic).")
    con = sqlite3.connect(DB)
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT substr(collected_at,1,10) FROM fd_lines WHERE sport='wnba' ORDER BY 1")]
    kinds = [r[0] for r in con.execute("SELECT DISTINCT snap_kind FROM fd_lines")]
    con.close()
    print(f"archive: {len(dates)} game-dates, brackets={kinds}\n")
    for kind in [k for k in ("tip", "opener") if k in kinds]:
        band = "CONSERVATIVE (post-reprice)" if kind == "tip" else "OPTIMISTIC (pre-news opener)"
        print(f"══ bracket: {kind}  — {band} ══")
        g = WB.run(dates=dates, fd_db=str(DB), snap_kind=kind)
        spots = _dedup(g)
        _tally(spots, f"ALL {len(spots)} spots")
        overs = [r for r in spots if r[4] is not None and r[8] is not None]  # graded set (win=over hit)
        _tally(overs, "  (graded overs)")
        by = defaultdict(list)
        for r in spots:
            by[r[3]].append(r)
        for stat, rs in sorted(by.items()):
            _tally(rs, f"  {stat}")
        print()
    print("Read the ROW between the two brackets as the honest edge; the tip bracket is the number")
    print("you can defend, the opener is the ceiling. Direction/by-stat should agree across both.")


# ── self-test (offline, no key) ────────────────────────────────────────────────────────────────
def cmd_selftest(a):
    fixture = {"away_team": "New York Liberty", "home_team": "Las Vegas Aces", "bookmakers": [
        {"key": "fanduel", "markets": [
            {"key": "player_points", "outcomes": [
                {"name": "Over", "description": "Sabrina Ionescu", "price": 1.87, "point": 18.5},
                {"name": "Under", "description": "Sabrina Ionescu", "price": 1.95, "point": 18.5}]},
            {"key": "player_rebounds_assists", "outcomes": [
                {"name": "Over", "description": "Sabrina Ionescu", "price": 1.90, "point": 9.5}]}]},
        {"key": "draftkings", "markets": [{"key": "player_points", "outcomes": [
            {"name": "Over", "description": "Sabrina Ionescu", "price": 1.83, "point": 18.5}]}]},
        {"key": "betmgm", "markets": [{"key": "player_points", "outcomes": [
            {"name": "Over", "description": "X", "price": 1.9, "point": 18.5}]}]},
    ]}
    rows = parse_event_odds(fixture, "2025-06-15", "tip")
    ok = True
    books = {r["book"] for r in rows}
    stats = {r["stat"] for r in rows}
    print(f"parsed {len(rows)} rows, books={books} (betmgm dropped), stats={stats}")
    ok &= books == {"fanduel", "draftkings"}
    ok &= "pts_reb" not in stats and "reb_ast" in stats     # combo key mapping correct
    ok &= all(r["collected_at"][:10] == "2025-06-15" for r in rows)

    # round-trip: write to a temp archive, read back through the REAL historical_props (with snap_kind)
    import tempfile
    import wnba_backtest as WB
    tmp = Path(tempfile.mkdtemp()) / "t.sqlite"
    c = sqlite3.connect(tmp)
    c.execute("""CREATE TABLE fd_lines(sport TEXT, event TEXT, player TEXT, stat TEXT, line REAL,
        side TEXT, odds REAL, book TEXT, collected_at TEXT, snap_kind TEXT)""")
    for r in rows:
        c.execute("INSERT INTO fd_lines VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (r["sport"], r["event"], r["player"], r["stat"], r["line"], r["side"],
                   r["odds"], r["book"], r["collected_at"], r["snap_kind"]))
    c.commit(); c.close()
    hp = WB.historical_props("Sabrina Ionescu", "2025-06-15", fd_db=str(tmp), snap_kind="tip")
    print(f"historical_props round-trip: {hp}")
    # points 18.5 should carry best over across FD(1.87)/DK(1.83) = 1.87, and the under 1.95
    ok &= "points" in hp and 18.5 in hp["points"]
    ok &= abs(hp["points"][18.5][0] - 1.87) < 1e-6 and abs(hp["points"][18.5][1] - 1.95) < 1e-6
    ok &= "reb_ast" in hp
    # wrong bracket returns nothing
    ok &= WB.historical_props("Sabrina Ionescu", "2025-06-15", fd_db=str(tmp), snap_kind="opener") == {}
    print("\nSELF-TEST", "PASS ✅ — parse + archive round-trip into historical_props correct"
          if ok else "FAIL ❌")
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate"); e.add_argument("--start", required=True); e.add_argument("--end", required=True)
    e.add_argument("--games-per-day", type=int, default=6); e.add_argument("--all-markets", action="store_true")
    e.add_argument("--opener", action="store_true"); e.set_defaults(fn=cmd_estimate)
    f = sub.add_parser("fetch"); f.add_argument("--start", required=True); f.add_argument("--end", required=True)
    f.add_argument("--sleep", type=float, default=0.3); f.add_argument("--refresh", action="store_true")
    f.add_argument("--all-markets", action="store_true"); f.add_argument("--opener", action="store_true")
    f.set_defaults(fn=cmd_fetch)
    b = sub.add_parser("backtest"); b.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("self-test"); s.set_defaults(fn=cmd_selftest)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
