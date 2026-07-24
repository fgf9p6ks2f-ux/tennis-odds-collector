#!/usr/bin/env python3
"""Backtest the MLB outs-under model on a REAL FanDuel/DraftKings historical-odds archive pulled from
The Odds API (https://the-odds-api.com). Built to break the sample-size ceiling: the live model is
validated on ~48 bets / ~11 real FD lines because we've only collected FD/DK outs since ~7/20/2026.
The Odds API holds player-prop snapshots at 5-minute cadence back to 2023-05-03 — three seasons of
REAL FD/DK pitcher-outs lines, exactly the "real lines only" input the live edge is judged on.

TWO STAGES, on purpose (so you subscribe for ONE month, pull once, then iterate offline forever):

  estimate  — OFFLINE. Print the credit cost of a fetch so you can size it to your plan's monthly
              quota BEFORE paying. No API key needed.
  fetch     — PAID. Pull historical events + pitcher_outs snapshots into mlb_odds_hist.sqlite.
              Resumable (skips days/events already stored); logs remaining quota after every call.
  backtest  — OFFLINE, FREE, re-runnable. Join the local archive to actual outs (statsapi) and score
              the exact live model: away + contact opp + route A (line > recent-5 median outs),
              capped at u15.5, with ppo as the shadow subset. Reports by line, by book, by route.
  self-test — OFFLINE. Run the parser+grader+model against a synthetic snapshot to prove the offline
              half works with no key. Run this first.

WHY MLB backtests cleanly (and WNBA only partially): the outs edge is STRUCTURAL — route A is "the
book posted him above his own recent form", computed hours before first pitch, so a 5-min snapshot of
the FD/DK line is a faithful input and the result (outs recorded) grades from the box score. No
latency/fill problem like the WNBA speed edge. [[feedback_real_lines_only]]

Usage:
  export THE_ODDS_API_KEY=...            # from the-odds-api.com dashboard (free tier confirms keys)
  python3 mlb_odds_backtest.py self-test
  python3 mlb_odds_backtest.py estimate --start 2023-05-03 --end 2025-10-01
  python3 mlb_odds_backtest.py fetch    --start 2025-04-01 --end 2025-10-01
  python3 mlb_odds_backtest.py backtest
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import statistics as st
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "mlb_odds_hist.sqlite"
API = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
KEY_ENV = "THE_ODDS_API_KEY"

# ── model constants (mirror k_paper.py / dashboard.py exactly so results are comparable) ─────────
CONTACT_MAX = 0.225      # opp season K% must be < this (whiff offenses excluded — the whole edge)
OUTS_CAP = 15.5          # board flags at or below this (16.5 demoted to shadow 2026-07-24)
SHADOW_CAP = 16.5        # logged/scored but not flagged
PPO_MED = 5.57           # shadow pitches-per-out threshold (the surviving new-B candidate)
BOOKS = ("fanduel", "draftkings")

# The Odds API documented quota cost (verify against current docs before a big pull):
#   additional markets (player props) = 10 credits per region per market per event.
#   the historical EVENTS list is cheap (treated as ~1 here). If the doc changes, edit these two.
CREDIT_PER_EVENT_ODDS = 10       # us region × pitcher_outs × 1 event
CREDIT_PER_EVENTS_LIST = 1


# ── HTTP (only used by `fetch`; offline stages never import requests) ────────────────────────────
def _key() -> str:
    k = os.environ.get(KEY_ENV)
    if not k:
        sys.exit(f"set {KEY_ENV} (from the-odds-api.com). `self-test` and `estimate` need no key.")
    return k


def _get(path: str, **params):
    """GET with quota-header logging. Returns (json, remaining, used)."""
    import requests
    params["apiKey"] = _key()
    r = requests.get(f"{API}{path}", params=params, timeout=40)
    rem = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    if r.status_code != 200:
        # 422 = no data at that snapshot (common for off-hours) — caller treats as empty, not fatal.
        if r.status_code == 422:
            return None, rem, used
        raise RuntimeError(f"{r.status_code} {r.url.split('apiKey=')[0]} :: {r.text[:200]}")
    return r.json(), rem, used


# ── storage ──────────────────────────────────────────────────────────────────────────────────
def _con() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS snap(
        event_id TEXT, snapshot_ts TEXT, commence_time TEXT, home_team TEXT, away_team TEXT,
        book TEXT, player TEXT, side TEXT, line REAL, price REAL, fetched_at TEXT,
        PRIMARY KEY(event_id, book, player, side, line))""")
    c.execute("""CREATE TABLE IF NOT EXISTS fetch_log(
        kind TEXT, ref TEXT, ts TEXT, note TEXT, PRIMARY KEY(kind, ref))""")
    return c


def _logged(c, kind, ref) -> bool:
    return c.execute("SELECT 1 FROM fetch_log WHERE kind=? AND ref=?", (kind, ref)).fetchone() is not None


def _mark(c, kind, ref, note=""):
    c.execute("INSERT OR REPLACE INTO fetch_log VALUES(?,?,?,?)",
              (kind, ref, dt.datetime.utcnow().isoformat(timespec="seconds"), note))


# ── parsing (pure — the testable core) ─────────────────────────────────────────────────────────
def parse_event_odds(data: dict) -> list[dict]:
    """Flatten one historical event-odds `data` blob into per-outcome rows for FD/DK pitcher_outs.
    The Odds API player-prop shape: outcome.name in {Over,Under}, outcome.description = pitcher,
    outcome.point = line, outcome.price = decimal odds."""
    rows = []
    ev = data or {}
    eid, ct = ev.get("id"), ev.get("commence_time")
    home, away = ev.get("home_team"), ev.get("away_team")
    for bk in ev.get("bookmakers") or []:
        if bk.get("key") not in BOOKS:
            continue
        for mk in bk.get("markets") or []:
            if mk.get("key") != "pitcher_outs":
                continue
            for o in mk.get("outcomes") or []:
                side = (o.get("name") or "").lower()           # "over"/"under"
                if side not in ("over", "under"):
                    continue
                if o.get("point") is None or o.get("price") is None:
                    continue
                rows.append({"event_id": eid, "commence_time": ct, "home_team": home,
                             "away_team": away, "book": bk["key"], "player": o.get("description"),
                             "side": side, "line": float(o["point"]), "price": float(o["price"])})
    return rows


# ── date helpers ─────────────────────────────────────────────────────────────────────────────
def _drange(start: str, end: str):
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    d = d0
    while d <= d1:
        yield d
        d += dt.timedelta(days=1)


def _in_season(d: dt.date) -> bool:
    return 3 <= d.month <= 10                 # late-March through October (regular season + playoffs)


# ── estimate (offline) ─────────────────────────────────────────────────────────────────────────
def cmd_estimate(a):
    days = [d for d in _drange(a.start, a.end) if _in_season(d)]
    games = a.games_per_day * len(days)
    credits = games * CREDIT_PER_EVENT_ODDS + len(days) * CREDIT_PER_EVENTS_LIST
    print(f"range {a.start} → {a.end}: {len(days)} in-season days, ~{games} events "
          f"(@ {a.games_per_day}/day)")
    print(f"estimated credits: {credits:,}")
    print(f"   = {games:,} event-odds calls × {CREDIT_PER_EVENT_ODDS} "
          f"+ {len(days)} events-list calls × {CREDIT_PER_EVENTS_LIST}")
    print("\nMatch this to your plan's MONTHLY quota. If a full 3-season pull exceeds one month,")
    print("fetch one season now and the rest next cycle (the archive is cumulative + resumable).")
    print("⚠ verify CREDIT_PER_EVENT_ODDS against current the-odds-api.com docs before a big pull.")


# ── fetch (paid) ───────────────────────────────────────────────────────────────────────────────
def cmd_fetch(a):
    c = _con()
    total_new = 0
    for d in _drange(a.start, a.end):
        if not _in_season(d):
            continue
        dref = d.isoformat()
        if _logged(c, "day", dref) and not a.refresh:
            continue
        # snapshot the slate mid-afternoon UTC (covers day + night games posted by then)
        ev_json, rem, used = _get(f"/historical/sports/{SPORT}/events", date=f"{dref}T16:00:00Z")
        events = ((ev_json or {}).get("data")) or []
        print(f"{dref}: {len(events)} events  [quota remaining {rem}, used {used}]", flush=True)
        for ev in events:
            eid, ct = ev.get("id"), ev.get("commence_time")
            if not eid or _logged(c, "event", eid):
                continue
            # pull the line ~`lead` min before first pitch. Default 25, NOT 90: the free-tier probe
            # (2026-07-24) proved FanDuel posts pitcher_OUTS late (it had FD strikeouts but no FD outs
            # 18h out) — a 90-min snapshot would catch DK but MISS FanDuel, and the live 22-4 is
            # FD-anchored. Close to tip captures FD; outs lines barely move on late scratches, so the
            # structural line>recent-5 signal is intact. --lead tunes it if FD posts even later.
            when = ct
            try:
                when = (dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        - dt.timedelta(minutes=a.lead)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                pass
            od_json, rem, used = _get(f"/historical/sports/{SPORT}/events/{eid}/odds",
                                      date=when, regions="us", markets="pitcher_outs",
                                      oddsFormat="decimal", bookmakers=",".join(BOOKS))
            snap_ts = (od_json or {}).get("timestamp")
            rows = parse_event_odds((od_json or {}).get("data") or {})
            now = dt.datetime.utcnow().isoformat(timespec="seconds")
            for rw in rows:
                c.execute("INSERT OR REPLACE INTO snap VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (rw["event_id"], snap_ts, rw["commence_time"], rw["home_team"],
                           rw["away_team"], rw["book"], rw["player"], rw["side"],
                           rw["line"], rw["price"], now))
            total_new += len(rows)
            _mark(c, "event", eid, f"{len(rows)} rows")
            c.commit()
            time.sleep(a.sleep)
        _mark(c, "day", dref, f"{len(events)} events")
        c.commit()
    print(f"done. {total_new} new outcome rows in {DB.name}")


# ── backtest (offline) ───────────────────────────────────────────────────────────────────────
def _score_start(gamelog: list[dict], game_date: str):
    """From a pitcher's full-season gamelog, locate the bet game (±1 day) and compute the model's
    point-in-time inputs from PRIOR starts only. Returns dict or None. Pure vs statsapi cache."""
    try:
        ld = dt.date.fromisoformat(game_date)
    except (TypeError, ValueError):
        return None
    g, bd = None, None
    for x in gamelog:
        if (x.get("bf") or 0) < 5 or not x.get("date"):
            continue
        try:
            dd = abs((dt.date.fromisoformat(x["date"]) - ld).days)
        except ValueError:
            continue
        if dd <= 1 and (bd is None or dd < bd):
            bd, g = dd, x
    if not g:
        return None
    prior = sorted([x for x in gamelog if x.get("date") and x["date"] < g["date"]
                    and (x.get("bf") or 0) >= 5], key=lambda x: x["date"])
    if len(prior) < 3:
        return None
    outs5 = [x["outs"] for x in prior[-5:]]
    r5 = st.median(outs5)
    po = [(x.get("pitches") or 0, x.get("outs") or 0) for x in prior[-5:] if (x.get("pitches") or 0) > 0]
    tot_o = sum(o for _, o in po)
    ppo = (sum(p for p, _ in po) / tot_o) if tot_o else None
    return {"actual": g["outs"], "is_home": g.get("is_home"), "opp_id": g.get("opp_id"),
            "r5": r5, "ppo": ppo}


def cmd_backtest(a):
    from mlb import data as MD
    c = _con()
    # one under row per (event, pitcher, book) — the freshest snapshot's line/price
    rows = c.execute(
        "SELECT event_id, commence_time, home_team, away_team, book, player, line, price "
        "FROM snap WHERE side='under'").fetchall()
    if not rows:
        sys.exit(f"{DB.name} is empty — run `fetch` first (or `self-test` to check the logic).")

    kcache, glcache, idcache = {}, {}, {}

    def team_k(season):
        if season not in kcache:
            try:
                kcache[season] = MD.team_kpct(season)[0]
            except Exception:
                kcache[season] = {}
        return kcache[season]

    def gamelog(name, season):
        pid = idcache.get(name)
        if pid is None and name not in idcache:
            pid = MD.find_pitcher(name)
            idcache[name] = pid
        if not pid:
            return []
        if pid not in glcache:
            try:
                glcache[pid] = MD.pitcher_gamelog(pid, season)
            except Exception:
                glcache[pid] = []
        return glcache[pid]

    bets = []
    for eid, ct, home, away, book, player, line, price in rows:
        if not player or line is None or line > SHADOW_CAP:
            continue
        season = int((ct or "2025")[:4])
        sc = _score_start(gamelog(player, season), (ct or "")[:10])
        if not sc:
            continue
        oppk = team_k(season).get(sc["opp_id"])
        away_start = (sc["is_home"] is False)
        # gates identical to the live board
        if not away_start or oppk is None or oppk >= CONTACT_MAX:
            continue
        route_a = line > sc["r5"]
        won = sc["actual"] < line                      # UNDER wins if outs recorded < line
        bets.append({"eid": eid, "book": book, "player": player, "line": line, "price": price,
                     "actual": sc["actual"], "r5": sc["r5"], "ppo": sc["ppo"],
                     "route_a": route_a, "oppk": oppk, "won": won})

    # dedupe each (player, event) preferring FanDuel (the book we actually bet), like _mlb_graded
    best = {}
    for b in bets:
        k = (b["player"], b["eid"])
        if k not in best or b["book"] == "fanduel":
            best[k] = b
    ded = list(best.values())

    def rec(rs):
        w = sum(1 for r in rs if r["won"])
        n = len(rs)
        if not n:
            return "  0-0"
        # +1u to (price-1) on a win, -1u on a loss
        u = sum((r["price"] - 1) if r["won"] else -1 for r in rs)
        return f"{w}-{n-w} ({100*w/n:3.0f}%, {u:+.1f}u)"

    def fd(rs):
        return [r for r in rs if r["book"] == "fanduel"]

    print(f"archive: {len(rows)} under rows → {len(ded)} deduped away+contact bets "
          f"(from {len({r[0] for r in rows})} events)\n")
    A = [r for r in ded if r["route_a"]]
    Acap = [r for r in A if r["line"] <= OUTS_CAP]
    App = [r for r in Acap if r["ppo"] is not None and r["ppo"] >= PPO_MED]
    print("=== the model, on real historical FD/DK lines ===")
    print(f"  base away+contact ≤{SHADOW_CAP:g}   {rec(ded):22s}  [FD {rec(fd(ded))}]")
    print(f"  + route A (line>r5)      {rec(A):22s}  [FD {rec(fd(A))}]")
    print(f"  + cap ≤{OUTS_CAP:g}  (LIVE MODEL)  {rec(Acap):22s}  [FD {rec(fd(Acap))}]")
    print(f"  + ppo≥{PPO_MED} (shadow)     {rec(App):22s}  [FD {rec(fd(App))}]")
    print("\n=== route-A by line ===")
    for ln in (14.5, 15.5, 16.5):
        s = [r for r in A if abs(r["line"] - ln) < .01]
        print(f"  u{ln:<5g} {rec(s):22s}  [FD {rec(fd(s))}]")
    print(f"\n  ⚠ opp K% is full-season here (mild leak, same as the live daily cache). "
          f"Sample above is the real-line test the live 22-4 couldn't be.")


# ── self-test (offline, no key) ────────────────────────────────────────────────────────────────
def cmd_selftest(a):
    fixture = {"timestamp": "2025-06-01T22:30:00Z", "data": {
        "id": "evt1", "commence_time": "2025-06-02T00:00:00Z",
        "home_team": "San Francisco Giants", "away_team": "Los Angeles Angels",
        "bookmakers": [
            {"key": "fanduel", "title": "FanDuel", "markets": [{"key": "pitcher_outs", "outcomes": [
                {"name": "Over", "description": "Grayson Rodriguez", "price": 2.10, "point": 15.5},
                {"name": "Under", "description": "Grayson Rodriguez", "price": 1.81, "point": 15.5}]}]},
            {"key": "draftkings", "title": "DK", "markets": [{"key": "pitcher_outs", "outcomes": [
                {"name": "Under", "description": "Grayson Rodriguez", "price": 1.74, "point": 15.5}]}]},
            {"key": "betmgm", "title": "BetMGM", "markets": [{"key": "pitcher_outs", "outcomes": [
                {"name": "Under", "description": "X", "price": 1.9, "point": 15.5}]}]},
        ]}}
    rows = parse_event_odds(fixture["data"])
    ok = True
    books = {r["book"] for r in rows}
    print(f"parsed {len(rows)} rows, books={books} (betmgm should be dropped)")
    ok &= books == {"fanduel", "draftkings"}
    ok &= any(r["side"] == "under" and r["line"] == 15.5 and r["price"] == 1.81
              and r["player"] == "Grayson Rodriguez" for r in rows if r["book"] == "fanduel")

    # scoring: a synthetic prior-starts log where r5 median = 12, actual = 12 (under 15.5 wins)
    gl = [{"date": f"2025-05-0{i}", "outs": o, "bf": 20, "pitches": 90, "opp_id": 137, "is_home": False}
          for i, o in enumerate([11, 16, 7, 16, 12], start=1)]
    gl.append({"date": "2025-06-02", "outs": 12, "bf": 22, "pitches": 87, "opp_id": 137,
               "is_home": False})
    sc = _score_start(gl, "2025-06-02")
    print(f"score: r5={sc['r5']} (want 12), actual={sc['actual']}, away={sc['is_home'] is False}, "
          f"ppo={sc['ppo']:.2f}")
    ok &= sc["r5"] == 12 and sc["actual"] == 12 and sc["is_home"] is False
    route_a = 15.5 > sc["r5"]
    won = sc["actual"] < 15.5
    print(f"route_a={route_a} (want True), under_won={won} (want True)")
    ok &= route_a and won
    print("\nSELF-TEST", "PASS ✅ — offline parse/score/grade all correct" if ok else "FAIL ❌")
    sys.exit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate"); e.add_argument("--start", required=True); e.add_argument("--end", required=True)
    e.add_argument("--games-per-day", type=int, default=13); e.set_defaults(fn=cmd_estimate)
    f = sub.add_parser("fetch"); f.add_argument("--start", required=True); f.add_argument("--end", required=True)
    f.add_argument("--sleep", type=float, default=0.3); f.add_argument("--refresh", action="store_true")
    f.add_argument("--lead", type=int, default=25, help="minutes before tip to snapshot "
                   "(low = catches FanDuel's late outs posting; the live edge is FD-anchored)")
    f.set_defaults(fn=cmd_fetch)
    b = sub.add_parser("backtest"); b.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("self-test"); s.set_defaults(fn=cmd_selftest)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
