"""Standalone Pinnacle MLB pitcher-strikeout prop collector (for GitHub Actions).
Self-contained: reuses the sibling pinnacle.py. Writes mlb_kprops.sqlite (env MLB_DB).

Strikeout props are "{Pitcher} (Total Strikeouts)" specials; alt lines live in the
per-matchup markets, not the bulk feed.
"""
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pinnacle  # noqa: E402

DB = Path(os.environ.get("MLB_DB", Path(__file__).resolve().parent / "mlb_kprops.sqlite"))


def collect():
    matchups = pinnacle._get("/sports/3/matchups")
    props = [m for m in matchups
             if "trikeout" in ((m.get("special") or {}).get("description") or "")]
    out = []
    for mu in props:
        pitcher = mu["special"]["description"].split("(")[0].strip()
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
                out.append((pitcher, line, o, u, mu.get("startTime")))
    return out


def main():
    ts = dt.datetime.now().replace(microsecond=0).isoformat()
    recs = collect()
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS k_props (
        collected_at TEXT, pitcher TEXT, line REAL, over_odds REAL, under_odds REAL,
        start_time TEXT, PRIMARY KEY (collected_at, pitcher, line))""")
    con.executemany("INSERT OR REPLACE INTO k_props VALUES (?,?,?,?,?,?)",
                    [(ts, *r) for r in recs])
    con.commit()
    con.close()
    pitchers = len({r[0] for r in recs})
    print(f"[{ts}] collected {len(recs)} K-prop lines / {pitchers} pitchers -> {DB}", flush=True)


if __name__ == "__main__":
    main()
