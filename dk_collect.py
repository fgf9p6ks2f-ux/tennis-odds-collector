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

# DK subcategory name -> our stat key (must match Pinnacle collectors' keys)
STAT_MAP = {
    "strikeouts thrown": "strikeouts", "total bases": "total_bases", "hits": "hits",
    "home runs": "home_runs", "rbis": "rbis", "stolen bases": "stolen_bases",
    "points": "points", "rebounds": "rebounds", "assists": "assists",
    "threes made": "threes", "3-pointers made": "threes",
    "pts + reb + ast": "pra", "points + rebounds + assists": "pra",
    "pts + reb": "pts_reb", "pts + ast": "pts_ast", "reb + ast": "reb_ast",
}
LEAGUES = {"mlb": "84240", "wnba": "94682"}


def _get(url):
    r = cr.get(url, impersonate="chrome124", timeout=30, headers=H)
    r.raise_for_status()
    return r.json()


def _dec(sel):
    try:
        return float(sel["displayOdds"]["decimal"])
    except (KeyError, TypeError, ValueError):
        return None


def collect_league(sport, lg):
    root = _get(f"{B}/leagues/{lg}")
    subs = {s["id"]: (s["name"], s.get("categoryId")) for s in root.get("subcategories", [])}
    events = {e["id"]: e.get("name", "") for e in root.get("events", [])}
    rows = []
    for sid, (sname, cat) in subs.items():
        key = STAT_MAP.get(sname.strip().lower())
        if not key or cat is None:
            continue
        try:
            j = _get(f"{B}/leagues/{lg}/categories/{cat}/subcategories/{sid}")
        except Exception:
            continue
        mkts = {m["id"]: m for m in j.get("markets", [])}
        for sel in j.get("selections", []):
            m = mkts.get(sel.get("marketId"))
            if not m:
                continue
            label = (sel.get("label") or "").strip().lower()
            side = "over" if label.startswith("over") else "under" if label.startswith("under") else None
            pts, dec = sel.get("points"), _dec(sel)
            if side is None or pts is None or dec is None:
                continue
            # player name = market name minus the stat suffix ("Aaron Judge Total Bases")
            player = re.sub(r"\s+(o/u|over/under|total bases|strikeouts.*|hits|home runs|"
                            r"rbis|stolen bases|points|rebounds|assists|threes.*|"
                            r"pts.*|reb.*)\s*$", "", m.get("name", ""), flags=re.I).strip()
            if not player:
                continue
            rows.append((sport, events.get(m.get("eventId"), ""), player, key,
                         float(pts), side, dec))
    return rows


def main():
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS fd_lines (
        collected_at TEXT, sport TEXT, event TEXT, player TEXT, stat TEXT, line REAL,
        side TEXT, odds REAL)""")
    cols = {c[1] for c in con.execute("PRAGMA table_info(fd_lines)")}
    if "book" not in cols:
        con.execute("ALTER TABLE fd_lines ADD COLUMN book TEXT DEFAULT 'fd'")
    total = 0
    for sport, lg in LEAGUES.items():
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
