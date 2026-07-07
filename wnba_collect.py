"""Standalone Pinnacle WNBA player-prop collector (GitHub Actions).

Basketball = sport 4; filter league 'WNBA'. Each prop is its own `special` matchup
"{Player} ({Stat})"; read the special's OWN /markets/straight (type=total, period 0,
prices ordered [over, under]) — NOT related/straight (that returns the parent GAME's
lines, the bug that polluted the MLB collector). Writes wnba_props table (env WNBA_DB).

Props post gameday; an empty run just means no props are up yet. Any parenthetical label
we don't recognize is logged so the alias map can be extended.
"""
import datetime as dt
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pinnacle  # noqa: E402

DB = Path(os.environ.get("WNBA_DB", Path(__file__).resolve().parent / "wnba_props.sqlite"))
# normalize Pinnacle's parenthetical label -> our stat key (MUST match FanDuel's keys so
# the edge scan can join). Combos listed with their common spellings.
STAT_ALIASES = {
    "points+rebounds+assists": "pra", "pts+reb+ast": "pra", "points + rebounds + assists": "pra",
    "points+assists": "pts_ast", "pts+ast": "pts_ast", "points + assists": "pts_ast",
    "points+rebounds": "pts_reb", "pts+reb": "pts_reb", "points + rebounds": "pts_reb",
    "rebounds+assists": "reb_ast", "reb+ast": "reb_ast", "rebounds + assists": "reb_ast",
    "3 point fg": "threes", "three point fg": "threes", "threes": "threes",
    "threes made": "threes", "3-pointers made": "threes", "made threes": "threes",
    "points": "points", "rebounds": "rebounds", "assists": "assists",
}


def _stat(desc):
    """('{Player}', stat_key) from a '{Player} ({Label})' special description."""
    m = re.search(r"\(([^)]+)\)", desc)
    if not m:
        return None, None
    return desc[:m.start()].strip(), STAT_ALIASES.get(m.group(1).strip().lower())


def collect():
    matchups = pinnacle._get("/sports/4/matchups")
    out, unknown = [], set()
    for mu in matchups:
        if "WNBA" not in ((mu.get("league") or {}).get("name") or ""):
            continue
        desc = (mu.get("special") or {}).get("description") or ""
        if not desc:
            continue
        player, stat = _stat(desc)
        if not stat:
            if "(" in desc:
                unknown.add(desc)
            continue
        markets = pinnacle._get(f"/matchups/{mu['id']}/markets/straight")
        tot = next((mk for mk in markets if mk.get("type") == "total"
                    and mk.get("period") == 0 and len(mk.get("prices", [])) >= 2), None)
        if not tot:
            continue
        over, under = tot["prices"][0], tot["prices"][1]     # [over, under] by order
        line = over.get("points")
        o = pinnacle.american_to_decimal(over.get("price"))
        u = pinnacle.american_to_decimal(under.get("price"))
        if line is not None and o and u:
            out.append((player, stat, line, o, u, mu.get("startTime")))
    if unknown:
        print("unmapped WNBA prop labels (add to STAT_ALIASES):", list(unknown)[:12], flush=True)
    return out


def main():
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    recs = collect()
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS wnba_props (
        collected_at TEXT, player TEXT, stat TEXT, line REAL, over_odds REAL,
        under_odds REAL, start_time TEXT,
        PRIMARY KEY (collected_at, player, stat, line))""")
    con.executemany("INSERT OR REPLACE INTO wnba_props VALUES (?,?,?,?,?,?,?)",
                    [(ts, *r) for r in recs])
    con.commit()
    con.close()
    print(f"[{ts}] {len(recs)} WNBA prop lines {dict(Counter(r[1] for r in recs))} -> {DB}",
          flush=True)


if __name__ == "__main__":
    main()
