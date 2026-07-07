"""Standalone Pinnacle odds collector — portable to any always-on host.

Self-contained: needs only `requests` + `sqlite3` and the sibling pinnacle.py/store.py.
DB path: env var DB_PATH if set, else ./odds.sqlite next to this file. So the same file
works for a local run, a systemd host, and a Docker volume.
"""
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pinnacle  # noqa: E402
import store  # noqa: E402

DB = Path(os.environ.get("DB_PATH", Path(__file__).resolve().parent / "odds.sqlite"))


def main():
    ts = dt.datetime.now().replace(microsecond=0).isoformat()
    recs = pinnacle.collect()
    store.save_snapshot(recs, DB, ts)
    have_set = sum(1 for r in recs if r.get("set_under"))
    have_gm = sum(1 for r in recs if r.get("games_over"))
    print(f"[{ts}] collected {len(recs)} matches "
          f"({have_set} set totals, {have_gm} games totals) -> {DB}", flush=True)


if __name__ == "__main__":
    main()
