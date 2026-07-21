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
# order matters: "Hits Allowed" before "Hits" so the substring match resolves correctly
PROP_STATS = {"Total Strikeouts": "strikeouts", "Pitching Outs": "outs",
              "Hits Allowed": "hits_allowed", "Earned Runs": "earned_runs",
              "Total Bases": "total_bases", "Home Runs": "home_runs",
              "Stolen Bases": "stolen_bases", "RBIs": "rbis", "Hits": "hits"}

# Single-GAME prop line sanity (min, max inclusive). The label substring match above also matches
# team/season markets (e.g. a season "Home Runs" total leaks a 127.5 line into the game HR feed) —
# reject any line outside the plausible single-game range so junk never enters the table.
SANE_LINE = {"strikeouts": (0.5, 15.5), "outs": (3.5, 24.5), "hits_allowed": (0.5, 12.5),
             "earned_runs": (0.5, 9.5), "total_bases": (0.5, 8.5), "home_runs": (0.5, 3.5),
             "stolen_bases": (0.5, 4.5), "rbis": (0.5, 8.5), "hits": (0.5, 6.5)}


def collect():
    matchups = pinnacle._get("/sports/3/matchups")
    out = []
    skipped = 0
    for mu in matchups:
        desc = (mu.get("special") or {}).get("description") or ""
        stat = next((s for label, s in PROP_STATS.items() if label in desc), None)
        if not stat:
            continue
        player = desc.split("(")[0].strip()
        # the prop's real line is on the special's OWN markets, not related/straight
        # (related/straight returns the parent game's spreads/totals — a mislabel trap)
        markets = pinnacle._get(f"/matchups/{mu['id']}/markets/straight")
        tot = next((mk for mk in markets if mk.get("type") == "total"
                    and mk.get("period") == 0 and len(mk.get("prices", [])) >= 2), None)
        if not tot:
            continue
        over, under = tot["prices"][0], tot["prices"][1]   # [over, under] by order
        line = over.get("points")
        o = pinnacle.american_to_decimal(over.get("price"))
        u = pinnacle.american_to_decimal(under.get("price"))
        if line is None or not (o and u):
            continue
        lo, hi = SANE_LINE.get(stat, (None, None))
        if lo is not None and not (lo <= line <= hi):
            skipped += 1                                   # team/season market leaked in — reject
            continue
        out.append((player, stat, line, o, u, mu.get("startTime")))
    if skipped:
        print(f"  filtered {skipped} out-of-range prop line(s)", flush=True)
    return out


def main():
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
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
