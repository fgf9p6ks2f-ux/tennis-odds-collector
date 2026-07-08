"""BetMGM prop collector — a third soft book for the line-shopping ledger.

BetMGM runs the bwin CDS platform. The Ontario catalog (www.on.betmgm.ca/cds-api)
is reachable from a datacenter IP with browser TLS impersonation (curl_cffi), and
Ontario lines match Alberta's — so this is ready for the AB launch on 2026-07-13.

Two calls per sport:
  fixtures?sportIds=..            -> games (name 'A at B', id, startDate)
  fixture-offers?fixtureIds=id    -> optionMarkets named 'Player: Stat', each with
                                     Over/Under options carrying the line + price
Props post a few hours before first pitch, exactly the pre-game window we bet.

Writes fd_lines(..., book='betmgm') so bet_ledger's best-price flag() picks it up.
The access-id is the public brand identifier embedded in the site JS (env BETMGM_ID).
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import re
import sqlite3
from pathlib import Path

from curl_cffi import requests as cr

from wnba_edge_scan import canon

HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("FD_DB", HERE / "fanduel_props.sqlite"))
B = "https://www.on.betmgm.ca/cds-api/bettingoffer"
UUID = os.environ.get("BETMGM_ID", "35b959ca-7823-4e0f-8d47-b4eb80630d42")
AID = base64.b64encode(UUID.encode()).decode()
COMMON = (f"x-bwin-accessid={AID}&lang=en&country=CA&userCountry=CA"
          "&subdivision=CA-Ontario")
H = {"Accept": "application/json", "Referer": "https://www.on.betmgm.ca/"}

# BetMGM market name (the part after 'Player: ', lowercased) -> our stat key
# NOTE: 'strikeouts' is intentionally NOT here — the generic 'Player: Stat' market for
# strikeouts is a BATTER prop (Over 0.5/1.5); PITCHER strikeouts (incl. the alt ladder)
# are handled separately in _pitcher_k() so batter Ks never pollute the K model.
STAT_MAP = {
    "total bases": "total_bases", "hits": "hits", "home runs": "home_runs",
    "rbi's": "rbis", "rbis": "rbis", "stolen bases": "stolen_bases",
    "hits + runs + rbis": "hrr",
    "points": "points", "rebounds": "rebounds", "assists": "assists",
    "made threes": "threes", "3-pt made": "threes",
    "points + rebounds + assists": "pra", "points + rebounds": "pts_reb",
    "points + assists": "pts_ast", "rebounds + assists": "reb_ast",
}
SPORT_IDS = {"mlb": 23, "wnba": 7}       # bwin sportIds

# 'Kyle Harrison to record 5+ strikeouts' -> (Kyle Harrison, 5)
K_LADDER = re.compile(r"^(?P<pl>.+?)\s+to record\s+(?P<n>\d+)\+\s+strikeouts?$", re.I)
# 'Connor Prielipp: Strikeouts' -> Connor Prielipp  (main O/U; pitcher only, see below)
K_MAIN = re.compile(r"^(?P<pl>.+?):\s*Strikeouts$", re.I)


def _get(path):
    r = cr.get(f"{B}/{path}", impersonate="chrome124", timeout=30, headers=H)
    r.raise_for_status()
    return r.json()


def _line(opt_name):
    m = re.search(r"(\d+(?:\.\d+)?)", opt_name or "")
    return float(m.group(1)) if m else None


def _price(o):
    return ((o.get("price") or {}).get("odds"))


def _pitcher_k(oms, event):
    """Pitcher strikeout rows (ladder + main O/U). Two market shapes:
      'P to record N+ strikeouts'  Yes @ price      -> over (N-0.5)
      'P: Strikeouts'              Over/Under X.5    -> main line, PITCHERS ONLY
    A name is a pitcher iff it appears in a ladder market — that filter drops batter
    'P: Strikeouts' (Over 0.5) props, which would otherwise poison the K model."""
    rows, pitchers = [], set()
    for m in oms:                                    # first pass: ladder + pitcher set
        nm = (m.get("name") or {}).get("value") or ""
        lad = K_LADDER.match(nm)
        if not lad:
            continue
        pl = lad.group("pl").strip()
        n = int(lad.group("n"))
        pitchers.add(canon(pl))
        for o in m.get("options") or []:             # single 'Yes' option
            dec = _price(o)
            if dec:
                rows.append((event, pl, "strikeouts", n - 0.5, "over", float(dec)))
    for m in oms:                                    # second pass: main O/U for pitchers
        nm = (m.get("name") or {}).get("value") or ""
        mm = K_MAIN.match(nm)
        if not mm or canon(mm.group("pl")) not in pitchers:
            continue
        pl = mm.group("pl").strip()
        for o in m.get("options") or []:
            types = (o.get("parameters") or {}).get("optionTypes") or []
            side = "over" if "Over" in types else "under" if "Under" in types else None
            ln, dec = _line((o.get("name") or {}).get("value")), _price(o)
            if side and ln is not None and dec:
                rows.append((event, pl, "strikeouts", float(ln), side, float(dec)))
    # the ladder rung (e.g. '6+ K' = over 5.5) and the main line (over 5.5) collide;
    # keep the BEST price per (player, line, side) — best for the bettor, one row each
    best = {}
    for r in rows:
        k = (canon(r[1]), r[3], r[4])
        if k not in best or r[5] > best[k][5]:
            best[k] = r
    return list(best.values())


def collect_sport(sport, sid):
    fx = _get(f"fixtures?{COMMON}&offerMapping=None&fixtureTypes=Standard&state=Latest"
              f"&sportIds={sid}&skip=0&take=80").get("fixtures", [])
    games = [f for f in fx if " at " in (f.get("name", {}).get("value") or "")]
    rows = []
    for g in games:
        try:
            fo = _get(f"fixture-offers?{COMMON}&fixtureIds={g['id']}&offerMapping=All")
        except Exception:
            continue
        offers = fo.get("fixtureOffers") or []
        oms = offers[0].get("optionMarkets", []) if offers else []
        ev = g["name"]["value"]
        if sport == "mlb":                           # pitcher K ladder (its own parser)
            rows += [(sport, e, p, k, ln, sd, od)
                     for (e, p, k, ln, sd, od) in _pitcher_k(oms, ev)]
        for m in oms:
            nm = (m.get("name") or {}).get("value") or ""
            if ":" not in nm:
                continue
            player, _, stat = nm.partition(":")
            key = STAT_MAP.get(stat.strip().lower())
            if not key:
                continue
            for o in m.get("options") or []:
                types = (o.get("parameters") or {}).get("optionTypes") or []
                side = "over" if "Over" in types else "under" if "Under" in types else None
                ln = _line((o.get("name") or {}).get("value"))
                dec = _price(o)
                if side and ln is not None and dec:
                    rows.append((sport, ev, player.strip(), key,
                                 float(ln), side, float(dec)))
    return rows


def main():
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS fd_lines (
        collected_at TEXT, sport TEXT, event TEXT, player TEXT, stat TEXT, line REAL,
        side TEXT, odds REAL, book TEXT DEFAULT 'fd')""")
    if "book" not in {c[1] for c in con.execute("PRAGMA table_info(fd_lines)")}:
        con.execute("ALTER TABLE fd_lines ADD COLUMN book TEXT DEFAULT 'fd'")
    total = 0
    for sport, sid in SPORT_IDS.items():
        try:
            rows = collect_sport(sport, sid)
        except Exception as e:
            print(f"betmgm {sport}: skipped ({str(e)[:60]})")
            continue
        con.executemany("INSERT OR IGNORE INTO fd_lines (collected_at,sport,event,player,"
                        "stat,line,side,odds,book) VALUES (?,?,?,?,?,?,?,?,'betmgm')",
                        [(ts, *r) for r in rows])
        con.commit()
        total += len(rows)
        print(f"betmgm {sport}: {len(rows)} lines")
    con.close()
    print(f"[{ts}] BetMGM {total} lines -> {DB}")


if __name__ == "__main__":
    main()
