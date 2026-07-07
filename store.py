"""Append-only SQLite store for odds snapshots — this is our own accruing history.

Each collection run appends one row per match (keyed by collected_at + match_id), so
repeated runs build a time series per match, giving us opening→closing line movement
and, after the match, the closing line for backtests.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

COLS = ["collected_at", "match_id", "start_time", "league", "best_of", "p1", "p2",
        "ml1", "ml2", "set_total_line", "set_over", "set_under", "set_spread",
        "spr_home", "spr_away", "games_line", "games_over", "games_under"]


def _connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(f"""CREATE TABLE IF NOT EXISTS odds (
        {', '.join(c + (' INTEGER' if c in ('match_id', 'best_of') else
                        ' TEXT' if c in ('collected_at', 'start_time', 'league', 'p1', 'p2')
                        else ' REAL') for c in COLS)},
        PRIMARY KEY (collected_at, match_id))""")
    return con


def save_snapshot(records: list[dict], db_path: Path, collected_at: str) -> int:
    con = _connect(db_path)
    rows = [[collected_at if c == "collected_at" else r.get(c) for c in COLS]
            for r in records]
    con.executemany(f"INSERT OR REPLACE INTO odds ({','.join(COLS)}) "
                    f"VALUES ({','.join('?' * len(COLS))})", rows)
    con.commit()
    n = con.total_changes
    con.close()
    return n


def load(db_path: Path):
    import pandas as pd
    con = sqlite3.connect(db_path)
    df = pd.read_sql("SELECT * FROM odds", con)
    con.close()
    return df
