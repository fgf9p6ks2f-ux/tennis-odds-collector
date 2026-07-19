"""VM-side ingest of the Mac-published DK board (dk_board.json) into wnba_lines.sqlite.

Runs every loop cycle right after fd_collect. Inserts each published quote as a book='dk'
row in fd_lines — the exact shape the Actions-era DK collector used — so posted_props,
dashboard._book_prices (card logos/best price) and CLV consume it with zero changes.
Idempotent per publish: a state file remembers the last ingested board hash. Stale boards
(Mac asleep > 45 min) are skipped so "best price" never quotes a dead number.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOARD = HERE / "dk_board.json"
STATE = HERE / ".dk_ingest_state"
DB = Path(os.environ.get("FD_DB", HERE / "wnba_lines.sqlite"))
MAX_AGE_MIN = 45


def main():
    if not BOARD.exists():
        return
    try:
        b = json.loads(BOARD.read_text())
    except (ValueError, OSError):
        return
    ts = (b.get("ts") or "").rstrip("Z")
    try:
        age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(ts)).total_seconds() / 60
    except ValueError:
        return
    if age > MAX_AGE_MIN:
        print(f"dk board stale ({age:.0f}m) — skip")
        return
    h = b.get("hash", "")
    if STATE.exists() and STATE.read_text().strip() == h:
        return                                          # this publish already ingested
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS fd_lines(
        collected_at TEXT, sport TEXT, event TEXT, player TEXT, stat TEXT,
        line REAL, side TEXT, odds REAL, book TEXT)""")
    now = dt.datetime.utcnow().isoformat()[:19]
    n = 0
    for l in b.get("lines") or []:
        con.execute("INSERT INTO fd_lines(collected_at, sport, event, player, stat, line, "
                    "side, odds, book) VALUES(?,?,?,?,?,?,?,?, 'dk')",
                    (now, "wnba", l.get("event"), l.get("player"), l.get("stat"),
                     l.get("line"), l.get("side"), l.get("odds")))
        n += 1
    con.commit()
    con.close()
    STATE.write_text(h)
    print(f"dk ingest: {n} lines @ {ts}")


if __name__ == "__main__":
    main()
