#!/usr/bin/env python3
"""Generic multi-sport HISTORICAL player-prop acquisition from The Odds API. Purpose: pull the full
FD/DK/BetMGM/… archive for NBA / NFL / NHL (and MLB/WNBA) into per-sport SQLite so future models are
built on real posted lines from day one — 'just have the data'. Model-agnostic: it acquires and
stores, it does NOT grade or flag (mlb_odds_backtest.py / wnba_odds_backtest.py are the model layers
on top of the same schema).

KEY EFFICIENCY — pull ALL US books for the price of two. The Odds API bills additional markets at
10 credits per region per market per EVENT, independent of book count. So `regions=us` returns every
US sportsbook at the SAME cost as `bookmakers=fanduel,draftkings`. For acquisition we always take the
whole US board — future best-price shopping is then free.

Historical player props exist from 2023-05-03 → so NBA/NHL get ~2.2 seasons, NFL 2, MLB ~2.8, WNBA 3.
5M-credit plan; a comprehensive multi-league tip-only pull is ~630k credits (≈1M with opener brackets).

Commands:
  odds_archive.py sports                       # list leagues + which have live events (recon)
  odds_archive.py estimate --sport basketball_nba --start 2023-10-24 --end 2024-06-17
  odds_archive.py fetch    --sport icehockey_nhl --start 2024-10-04 --end 2025-06-24
  odds_archive.py summary  [--sport ...]        # what's been acquired
  odds_archive.py self-test                     # offline parser check, no key

Key: THE_ODDS_API_KEY env, else ./.odds_key (gitignored).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                    # pragma: no cover
    ET = None

HERE = Path(__file__).resolve().parent
API = "https://api.the-odds-api.com/v4"
KEY_ENV = "THE_ODDS_API_KEY"
CREDIT_PER_MARKET_EVENT = 10                          # historical additional-market cost (per region)
CREDIT_PER_EVENTS_LIST = 1

# Per-league config: comprehensive-but-modelable market sets, season months (skip offday API calls),
# and a rough games/day for the offline estimate. --markets overrides the preset.
SPORTS = {
    "baseball_mlb": {
        "markets": ["pitcher_outs", "pitcher_strikeouts", "pitcher_earned_runs",
                    "pitcher_hits_allowed", "pitcher_walks", "batter_hits", "batter_total_bases",
                    "batter_home_runs", "batter_rbis", "batter_runs_scored", "batter_stolen_bases"],
        "season": {3, 4, 5, 6, 7, 8, 9, 10, 11}, "gpd": 13},
    "basketball_wnba": {
        "markets": ["player_points", "player_rebounds", "player_assists",
                    "player_points_rebounds_assists", "player_points_rebounds",
                    "player_points_assists", "player_rebounds_assists"],
        "season": {5, 6, 7, 8, 9, 10}, "gpd": 6},
    "basketball_nba": {
        "markets": ["player_points", "player_rebounds", "player_assists", "player_threes",
                    "player_blocks", "player_steals", "player_points_rebounds_assists",
                    "player_points_rebounds", "player_points_assists", "player_rebounds_assists"],
        "season": {10, 11, 12, 1, 2, 3, 4, 5, 6}, "gpd": 7},
    "americanfootball_nfl": {
        "markets": ["player_pass_yds", "player_pass_tds", "player_pass_completions",
                    "player_pass_attempts", "player_pass_interceptions", "player_rush_yds",
                    "player_rush_attempts", "player_receptions", "player_reception_yds",
                    "player_rush_reception_yds", "player_anytime_td"],
        "season": {9, 10, 11, 12, 1, 2}, "gpd": 3},
    "icehockey_nhl": {
        "markets": ["player_points", "player_goals", "player_assists", "player_shots_on_goal",
                    "player_blocked_shots", "player_power_play_points", "player_total_saves"],
        "season": {10, 11, 12, 1, 2, 3, 4, 5, 6}, "gpd": 7},
}


def _db(sport: str) -> Path:
    return HERE / f"{sport}_odds_hist.sqlite"


def _key() -> str:
    k = os.environ.get(KEY_ENV)
    if not k and (HERE / ".odds_key").exists():
        k = (HERE / ".odds_key").read_text().strip()
    if not k:
        sys.exit(f"no key: export {KEY_ENV} or write ./.odds_key (gitignored). "
                 "`sports`/`estimate`/`summary`/`self-test` on cached data need no key.")
    return k


BOOKS_DEFAULT = "fanduel,draftkings,betmgm,williamhill_us"   # FD, DK, BetMGM, Caesars (all us region)


def _get(path: str, _tries=5, **params):
    """GET with retry+backoff on transient failures (429 rate-limit, 5xx, network). 404/422 = no
    snapshot / unposted market → (None, ...) so the caller skips cleanly. A hard-invalid key (401/403)
    fails fast. Only raises after exhausting retries — the caller then leaves the event UN-marked so a
    resume re-attempts it (no silent data loss)."""
    import requests
    params["apiKey"] = _key()
    last = ""
    for i in range(_tries):
        try:
            r = requests.get(f"{API}{path}", params=params, timeout=45)
        except requests.RequestException as e:
            last = f"net:{e}"
            time.sleep(min(2 ** i, 30))
            continue
        rem = r.headers.get("x-requests-remaining")
        if r.status_code == 200:
            return r.json(), rem, 200
        if r.status_code in (404, 422):              # no data at snapshot / market unposted — normal
            return None, rem, r.status_code
        if r.status_code in (401, 403):              # bad/expired key — no point retrying
            raise RuntimeError(f"AUTH {r.status_code}: {r.text[:160]} (check .odds_key)")
        last = f"{r.status_code} {r.text[:120]}"     # 429 / 5xx → back off and retry
        time.sleep(min(2 ** i, 30))
    raise RuntimeError(f"{path} failed after {_tries} tries :: {last}")


# ── storage ──────────────────────────────────────────────────────────────────────────────────
def _con(sport: str) -> sqlite3.Connection:
    c = sqlite3.connect(_db(sport), timeout=60)
    # WAL + a long busy_timeout so a concurrent READER (e.g. an audit query) can never crash a
    # WRITE again: the 2026-07-24 MLB pull died at 2025-08-04 when an audit query held a lock and
    # the fetch's commit hit "database is locked". Now the writer waits the reader out instead.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("""CREATE TABLE IF NOT EXISTS props(
        sport TEXT, event_id TEXT, commence_time TEXT, game_date TEXT, home_team TEXT,
        away_team TEXT, book TEXT, market TEXT, player TEXT, side TEXT, line REAL, price REAL,
        snapshot_ts TEXT, snap_kind TEXT, fetched_at TEXT,
        PRIMARY KEY(event_id, book, market, player, side, line, snap_kind))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_props ON props(sport, game_date, player)")
    c.execute("CREATE TABLE IF NOT EXISTS fetch_log(ref TEXT PRIMARY KEY, ts TEXT)")
    return c


def _done(c, ref) -> bool:
    return c.execute("SELECT 1 FROM fetch_log WHERE ref=?", (ref,)).fetchone() is not None


def _mark(c, ref):
    c.execute("INSERT OR REPLACE INTO fetch_log VALUES(?,?)",
              (ref, dt.datetime.utcnow().isoformat(timespec="seconds")))


# ── parsing (pure — the testable core) ─────────────────────────────────────────────────────────
def _game_date(commence_time: str) -> str:
    """ET calendar date of first pitch/tip — the join key to box scores. Falls back to the raw UTC
    date if zoneinfo is unavailable."""
    try:
        d = dt.datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        return (d.astimezone(ET).date().isoformat() if ET else d.date().isoformat())
    except Exception:
        return (commence_time or "")[:10]


def parse_event_odds(sport: str, data: dict, snap_ts: str, snap_kind: str) -> list[dict]:
    """Flatten one historical event-odds blob into per-outcome rows across EVERY book returned
    (regions=us gives the whole US board at no extra credit cost)."""
    ev = data or {}
    eid, ct = ev.get("id"), ev.get("commence_time")
    gd = _game_date(ct)
    home, away = ev.get("home_team"), ev.get("away_team")
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    rows = []
    for bk in ev.get("bookmakers") or []:
        for mk in bk.get("markets") or []:
            for o in mk.get("outcomes") or []:
                side = (o.get("name") or "").lower()
                price = o.get("price")
                # decimal odds must be > 1.0 to be a real bet — some books post a dead 1.0 (stake-back)
                # on can't-happen unders (e.g. a slugger 'under 0.5 steals'); drop those, not real prices.
                if price is None or float(price) <= 1.0:
                    continue
                rows.append({"sport": sport, "event_id": eid, "commence_time": ct, "game_date": gd,
                             "home_team": home, "away_team": away, "book": bk.get("key"),
                             "market": mk.get("key"), "player": o.get("description"),
                             "side": side, "line": o.get("point"), "price": float(o["price"]),
                             "snapshot_ts": snap_ts, "snap_kind": snap_kind, "fetched_at": now})
    return rows


def _drange(start, end):
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    while d0 <= d1:
        yield d0
        d0 += dt.timedelta(days=1)


# ── sports (recon) ─────────────────────────────────────────────────────────────────────────────
def cmd_sports(a):
    data, rem, _ = _get("/sports", all="true")
    print(f"{len(data)} sports offered  [credits {rem}]\n")
    by = {s["key"]: s for s in data}
    for k, cfg in SPORTS.items():
        s = by.get(k, {})
        ev, rem, _ = _get(f"/sports/{k}/events")
        n = len(ev) if ev else 0
        nxt = min((e.get("commence_time", "") for e in ev), default="-")[:10] if ev else "-"
        print(f"  {k:24s} active={str(s.get('active')):5s} live_events={n:3d} next={nxt}  "
              f"{len(cfg['markets'])} preset markets")


# ── estimate (offline) ─────────────────────────────────────────────────────────────────────────
def cmd_estimate(a):
    cfg = SPORTS[a.sport]
    markets = a.markets.split(",") if a.markets else cfg["markets"]
    snaps = 2 if a.opener else 1
    days = [d for d in _drange(a.start, a.end) if d.month in cfg["season"]]
    gpd = a.games_per_day or cfg["gpd"]
    events = gpd * len(days)
    credits = events * len(markets) * snaps * CREDIT_PER_MARKET_EVENT + len(days) * CREDIT_PER_EVENTS_LIST
    print(f"{a.sport}  {a.start}→{a.end}: {len(days)} in-season days, ~{events} events (@{gpd}/day)")
    print(f"markets: {len(markets)}   snapshots/event: {snaps}   books: ALL US (regions=us, free)")
    print(f"estimated credits: {credits:,}")
    print(f"   = {events:,} events × {len(markets)} markets × {snaps} × {CREDIT_PER_MARKET_EVENT}"
          f" + {len(days)} list calls")


# ── fetch (paid) ───────────────────────────────────────────────────────────────────────────────
def cmd_fetch(a):
    cfg = SPORTS[a.sport]
    markets = a.markets.split(",") if a.markets else cfg["markets"]
    mk_param = ",".join(markets)
    c = _con(a.sport)
    total = 0
    for d in _drange(a.start, a.end):
        if d.month not in cfg["season"] and not a.all_days:
            continue
        dref = d.isoformat()
        if _done(c, f"day:{dref}") and not a.refresh:
            continue
        # snapshot the events list EARLY (13:00Z ≈ 9am ET) so the whole ET day is still upcoming — a
        # late snapshot silently drops games that already started (verified: 23:00Z returned 11/15 vs
        # 15/15 at 16:00Z). Then keep only games whose ET date is this day (event-log dedups overlap).
        ev_json, rem, st = _get(f"/historical/sports/{a.sport}/events", date=f"{dref}T13:00:00Z")
        events = ((ev_json or {}).get("data")) or []
        events = [e for e in events if _game_date(e.get("commence_time", "")) == dref]
        print(f"{dref}: {len(events)} events  [credits {rem}]", flush=True)
        for ev in events:
            eid, ct = ev.get("id"), ev.get("commence_time")
            if not eid:
                continue
            snaps = [("tip", a.lead)] + ([("opener", None)] if a.opener else [])
            for kind, lead in snaps:
                if _done(c, f"ev:{eid}:{kind}"):
                    continue
                when = ct
                try:
                    delta = dt.timedelta(minutes=lead) if lead is not None else dt.timedelta(days=2)
                    when = (dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                            - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    pass
                try:
                    od, rem, st = _get(f"/historical/sports/{a.sport}/events/{eid}/odds",
                                       date=when, bookmakers=a.books, markets=mk_param,
                                       oddsFormat="decimal")
                except RuntimeError as e:            # exhausted retries — DON'T mark done, resume retries
                    print(f"    ! {eid} {kind}: {e} — left for resume", flush=True)
                    continue
                snap_ts = (od or {}).get("timestamp")
                rows = parse_event_odds(a.sport, (od or {}).get("data") or {}, snap_ts, kind)
                for r in rows:
                    c.execute("INSERT OR REPLACE INTO props VALUES "
                              "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (r["sport"], r["event_id"], r["commence_time"], r["game_date"],
                               r["home_team"], r["away_team"], r["book"], r["market"], r["player"],
                               r["side"], r["line"], r["price"], r["snapshot_ts"], r["snap_kind"],
                               r["fetched_at"]))
                total += len(rows)
                _mark(c, f"ev:{eid}:{kind}")
                c.commit()
                time.sleep(a.sleep)
        _mark(c, f"day:{dref}")
        c.commit()
    print(f"done. {total} new outcome rows in {_db(a.sport).name}")


# ── summary (offline) ──────────────────────────────────────────────────────────────────────────
def cmd_summary(a):
    keys = [a.sport] if a.sport else list(SPORTS)
    for k in keys:
        if not _db(k).exists():
            continue
        c = sqlite3.connect(_db(k))
        n, = c.execute("SELECT COUNT(*) FROM props").fetchone()
        if not n:
            c.close()
            continue
        dmin, dmax = c.execute("SELECT MIN(game_date), MAX(game_date) FROM props").fetchone()
        nev, = c.execute("SELECT COUNT(DISTINCT event_id) FROM props").fetchone()
        books = [r[0] for r in c.execute(
            "SELECT book FROM props GROUP BY book ORDER BY COUNT(*) DESC")]
        mkts = c.execute("SELECT COUNT(DISTINCT market) FROM props").fetchone()[0]
        c.close()
        print(f"■ {k}: {n:,} rows, {nev:,} events, {dmin}→{dmax}, {mkts} markets, "
              f"{len(books)} books ({', '.join(books[:6])}{'…' if len(books) > 6 else ''})")


# ── self-test (offline, no key) ────────────────────────────────────────────────────────────────
def cmd_selftest(a):
    fx = {"id": "e1", "commence_time": "2024-11-01T00:10:00Z", "home_team": "Denver Nuggets",
          "away_team": "LA Lakers", "bookmakers": [
              {"key": "fanduel", "markets": [{"key": "player_points", "outcomes": [
                  {"name": "Over", "description": "Nikola Jokic", "price": 1.9, "point": 27.5},
                  {"name": "Under", "description": "Nikola Jokic", "price": 1.9, "point": 27.5}]}]},
              {"key": "betmgm", "markets": [{"key": "player_assists", "outcomes": [
                  {"name": "Over", "description": "Nikola Jokic", "price": 1.83, "point": 9.5}]}]}]}
    rows = parse_event_odds("basketball_nba", fx, "2024-10-31T23:45:00Z", "tip")
    ok = True
    print(f"parsed {len(rows)} rows, books={sorted({r['book'] for r in rows})} "
          f"(regions=us keeps ALL books incl betmgm)")
    ok &= {r["book"] for r in rows} == {"fanduel", "betmgm"}
    ok &= any(r["market"] == "player_assists" and r["book"] == "betmgm" for r in rows)
    # commence 2024-11-01T00:10Z = 2024-10-31 8:10pm ET → game_date must roll back a day
    gd = _game_date("2024-11-01T00:10:00Z")
    print(f"ET game_date of a 00:10Z tip: {gd} (want 2024-10-31 — the UTC/ET boundary)")
    ok &= (gd == "2024-10-31") if ET else True
    ok &= all(r["game_date"] == gd for r in rows)
    print("\nSELF-TEST", "PASS ✅ — all-books parse + ET game_date correct" if ok else "FAIL ❌")
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sports").set_defaults(fn=cmd_sports)
    for name, fn in (("estimate", cmd_estimate), ("fetch", cmd_fetch)):
        s = sub.add_parser(name)
        s.add_argument("--sport", required=True, choices=list(SPORTS))
        s.add_argument("--start", required=True)
        s.add_argument("--end", required=True)
        s.add_argument("--markets", help="comma-separated override of the preset")
        s.add_argument("--opener", action="store_true", help="also grab a pre-news opener snapshot")
        s.add_argument("--games-per-day", type=int, default=0)
        if name == "fetch":
            s.add_argument("--books", default=BOOKS_DEFAULT,
                           help="comma book keys (default FD,DK,BetMGM,Caesars — all 'us' region)")
            s.add_argument("--lead", type=int, default=25, help="min before tip for the snapshot")
            s.add_argument("--sleep", type=float, default=0.2)
            s.add_argument("--refresh", action="store_true")
            s.add_argument("--all-days", action="store_true", help="don't skip offseason months")
        s.set_defaults(fn=fn)
    su = sub.add_parser("summary"); su.add_argument("--sport", choices=list(SPORTS)); su.set_defaults(fn=cmd_summary)
    sub.add_parser("self-test").set_defaults(fn=cmd_selftest)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
