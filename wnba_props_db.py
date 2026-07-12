"""Resolve the freshest WNBA prop-lines DB — the resilience layer for line snapshots.

Two writers land WNBA lines into an `fd_lines` table:
  - collect-odds COMMITS the full multi-sport fanduel_props.sqlite (~30MB) every ~30 min, BUT GitHub
    throttles/drops its schedule (3-4h gaps, sometimes 9h+), so the committed copy can go stale.
  - the 24/7 self-redispatching wnba-watch loop writes a small WNBA-only wnba_lines.sqlite IN-JOB
    every ~2 min (~8k rows / <1MB, gitignored local scratch — no commit churn). The dashboard and the
    CLV close-capture run inside that same job, so they read these fresh lines every cycle and bake
    them into the committed docs/index.html + wnba_clv.sqlite.

Every WNBA lines consumer (posted_props, the dashboard best-price tags, the CLV close-capture) calls
props_db() and reads whichever file holds the NEWEST wnba line — so prices and the CLV loop stay
current regardless of which collector last ran. On a cold checkout wnba_lines.sqlite may not exist
yet; the resolver then falls back to the committed full DB until the first in-job collection lands.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
FULL = HERE / "fanduel_props.sqlite"       # collect-odds' full multi-sport snapshot
WNBA = HERE / "wnba_lines.sqlite"          # the watch loop's small, always-fresh WNBA-only snapshot


def _max_ts(p):
    """Newest wnba collected_at in `p`, or None if missing/unreadable/no wnba rows."""
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ts = con.execute("SELECT MAX(collected_at) FROM fd_lines WHERE sport='wnba'").fetchone()[0]
        con.close()
        return ts
    except Exception:
        return None


def props_db():
    """Path (str) to the DB with the freshest WNBA lines. Falls back to the full DB path even if it
    doesn't exist yet, so callers can still do their own .exists() guard."""
    cands = [(p, _max_ts(p)) for p in (WNBA, FULL)]
    cands = [(p, t) for p, t in cands if t]
    if not cands:
        return str(FULL)
    return str(max(cands, key=lambda x: x[1])[0])
