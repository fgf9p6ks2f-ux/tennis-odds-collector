"""DraftKings prop collector — a second soft book vs the same Pinnacle sharp anchor.

Adding a book multiplies the direct edges the ledger can find (each book shades
different props) AND lets the ledger take the BEST price across books for the same
line. Writes into the same fd_lines table (sport, event, player, stat, line, side,
odds) with book='dk', so bet_ledger's existing flag() logic picks it up unchanged.

DK's public JSON sits behind Akamai TLS-fingerprint blocking, so we use curl_cffi's
browser impersonation (a normal requests/urllib UA gets 403). Endpoint:
  sportscontent/dkusco/v1/leagues/{lg}/categories/{cat}/subcategories/{sub}
returns markets + selections; we map each player O/U prop to our stat keys.

    python dk_collect.py            # MLB + WNBA current boards -> fd_lines(book='dk')
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from pathlib import Path

from curl_cffi import requests as cr

HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("FD_DB", HERE / "fanduel_props.sqlite"))
B = "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusco/v1"
H = {"Referer": "https://sportsbook.draftkings.com/", "Accept": "application/json"}

# DK subcategory name (lowercased, trailing ' o/u' stripped) -> our stat key. Must
# match the Pinnacle collectors' keys so the ledger joins them. Player identity comes
# from each selection's participants[0] (type='Player') — NOT the market name — so
# game/team totals ('Total Total Bases', team-inning markets) are excluded structurally.
STAT_MAP = {
    "strikeouts thrown": "strikeouts", "total bases": "total_bases", "hits": "hits",
    "hits + runs + rbis": "hrr",
    "home runs": "home_runs", "rbis": "rbis", "stolen bases": "stolen_bases",
    "points": "points", "rebounds": "rebounds", "assists": "assists",
    "threes": "threes", "threes made": "threes", "3-pointers made": "threes",
    "made threes": "threes",
    "pts + reb + ast": "pra", "points + rebounds + assists": "pra",
    "pts + reb": "pts_reb", "points + rebounds": "pts_reb",
    "pts + ast": "pts_ast", "points + assists": "pts_ast",
    "reb + ast": "reb_ast", "rebounds + assists": "reb_ast",
}
LEAGUES = {"mlb": "84240", "wnba": "94682"}


def _stat_key(subcat_name):
    return STAT_MAP.get(re.sub(r"\s*o/u\s*$", "", subcat_name.strip().lower()))


def _get(url):
    r = cr.get(url, impersonate="chrome124", timeout=30, headers=H)
    r.raise_for_status()
    return r.json()


def _dec(sel):
    try:
        return float(sel["displayOdds"]["decimal"])
    except (KeyError, TypeError, ValueError):
        return None


def _player(sel):
    """Player name from a selection's participants, or None if it isn't a player prop."""
    for p in sel.get("participants") or []:
        if p.get("type") == "Player" and p.get("name"):
            return p["name"]
    return None


def collect_league(sport, lg):
    root = _get(f"{B}/leagues/{lg}")
    subs = {s["id"]: (s["name"], s.get("categoryId")) for s in root.get("subcategories", [])}
    # PRE-GAME events only: skip STARTED/live games, whose in-play line churn was firing false
    # "opening line" alerts for the game being played right now. DK gives an explicit status;
    # fall back to startEventDate (7-digit fraction, so trim to seconds before parsing).
    now = dt.datetime.now(dt.timezone.utc)
    events = {}
    for e in root.get("events", []):
        st = (e.get("status") or "").upper()
        if st and st != "NOT_STARTED":
            continue
        if not st:
            sd = (e.get("startEventDate") or "")[:19]
            try:
                if sd and dt.datetime.fromisoformat(sd).replace(tzinfo=dt.timezone.utc) <= now:
                    continue
            except ValueError:
                pass
        events[e["id"]] = e.get("name", "")
    rows, seen = [], set()          # seen = (marketId, side) — dedupe repeated subcats
    for sid, (sname, cat) in subs.items():
        key = _stat_key(sname)
        if not key or cat is None:
            continue
        try:
            j = _get(f"{B}/leagues/{lg}/categories/{cat}/subcategories/{sid}")
        except Exception:
            continue
        emkt = {m["id"]: m.get("eventId") for m in j.get("markets", [])}
        for sel in j.get("selections", []):
            label = (sel.get("label") or "").strip().lower()
            side = "over" if label.startswith("over") else \
                   "under" if label.startswith("under") else None
            pts, dec, player = sel.get("points"), _dec(sel), _player(sel)
            if side is None or pts is None or dec is None or not player:
                continue
            k = (sel.get("marketId"), side)
            if k in seen:              # same player market surfaced in 'X' and 'X O/U'
                continue
            eid = emkt.get(sel.get("marketId"))
            if eid not in events:      # market's game is live/started (pre-game filter above) — skip
                continue
            seen.add(k)
            rows.append((sport, events[eid], player, key, float(pts), side, dec))
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--wnba", action="store_true", help="WNBA only (skip MLB) — for the resilient loop")
    leagues = {"wnba": LEAGUES["wnba"]} if ap.parse_args().wnba else LEAGUES
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS fd_lines (
        collected_at TEXT, sport TEXT, event TEXT, player TEXT, stat TEXT, line REAL,
        side TEXT, odds REAL)""")
    cols = {c[1] for c in con.execute("PRAGMA table_info(fd_lines)")}
    if "book" not in cols:
        con.execute("ALTER TABLE fd_lines ADD COLUMN book TEXT DEFAULT 'fd'")
    total = 0
    for sport, lg in leagues.items():
        try:
            rows = collect_league(sport, lg)
        except Exception as e:
            print(f"dk {sport}: skipped ({str(e)[:60]})")
            continue
        con.executemany("INSERT INTO fd_lines (collected_at,sport,event,player,stat,line,"
                        "side,odds,book) VALUES (?,?,?,?,?,?,?,?,'dk')",
                        [(ts, *r) for r in rows])
        con.commit()
        total += len(rows)
        print(f"dk {sport}: {len(rows)} lines")
    con.close()
    print(f"[{ts}] DraftKings {total} lines -> {DB}")


if __name__ == "__main__":
    main()
