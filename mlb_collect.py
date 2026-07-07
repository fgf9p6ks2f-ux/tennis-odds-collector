"""Standalone Pinnacle MLB pitcher-prop collector (GitHub Actions).
Grabs strikeouts AND pitching outs. Writes pitcher_props table (env MLB_DB).
Alt lines live in the per-matchup markets, not the bulk feed.
"""
import datetime as dt
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pinnacle  # noqa: E402

DB = Path(os.environ.get("MLB_DB", Path(__file__).resolve().parent / "mlb_kprops.sqlite"))
PROP_STATS = {"Total Strikeouts": "strikeouts", "Pitching Outs": "outs",
              "Total Bases": "total_bases", "Home Runs": "home_runs",
              "Hits Allowed": "hits_allowed", "Earned Runs": "earned_runs"}


def collect():
    matchups = pinnacle._get("/sports/3/matchups")
    out = []
    for mu in matchups:
        desc = (mu.get("special") or {}).get("description") or ""
        stat = next((s for label, s in PROP_STATS.items() if label in desc), None)
        if not stat:
            continue
        pitcher = desc.split("(")[0].strip()
        for mk in pinnacle._get(f"/matchups/{mu['id']}/markets/related/straight"):
            if mk.get("type") != "team_total" or mk.get("period") != 0:
                continue
            pr = {p.get("designation"): p for p in mk.get("prices", [])}
            over, under = pr.get("over"), pr.get("under")
            if not (over and under):
                continue
            line = over.get("points")
            o = pinnacle.american_to_decimal(over.get("price"))
            u = pinnacle.american_to_decimal(under.get("price"))
            if line and o and u:
                out.append((pitcher, stat, line, o, u, mu.get("startTime")))
    return out


def main():
    ts = dt.datetime.now().replace(microsecond=0).isoformat()
    recs = collect()
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS pitcher_props (
        collected_at TEXT, pitcher TEXT, stat TEXT, line REAL, over_odds REAL,
        under_odds REAL, start_time TEXT,
        PRIMARY KEY (collected_at, pitcher, stat, line))""")
    con.executemany("INSERT OR REPLACE INTO pitcher_props VALUES (?,?,?,?,?,?,?)",
                    [(ts, *r) for r in recs])
    con.commit()
    con.close()
    print(f"[{ts}] {len(recs)} prop lines {dict(Counter(r[1] for r in recs))} -> {DB}", flush=True)


if __name__ == "__main__":
    main()
