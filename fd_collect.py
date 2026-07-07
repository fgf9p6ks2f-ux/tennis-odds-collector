"""Standalone FanDuel MLB pitcher-prop collector (GitHub Actions, no device).

FanDuel's sbapi is reachable from GitHub US datacenter IPs (unlike DraftKings). Flow:
  content-managed-page?customPageId=mlb  -> game events (pitchers are in the event name)
  event-page?eventId=..                  -> layout.tabs
  event-page?eventId=..&tab=<slug>       -> prop markets (strikeouts / pitching outs)

Handles both FanDuel formats: "Over/Under X.5" markets and "X+ Strikeouts" alt lines
(X+ => Over (X-1).5). American odds -> decimal. Writes fanduel_props (env FD_DB).

FRAGILE: FanDuel rotates _AK and changes endpoints to break scrapers. When it stops
returning data, refresh _AK from the site's network calls.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import urllib.request
from collections import Counter
from pathlib import Path

AK = os.environ.get("FD_AK", "FhMFpcPWXMeyZxOx")
STATE = os.environ.get("FD_STATE", "ny")
BASE = f"https://sbapi.{STATE}.sportsbook.fanduel.com/api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DB = Path(os.environ.get("FD_DB", Path(__file__).resolve().parent / "fanduel_props.sqlite"))

STAT_KEYS = {"strikeout": "strikeouts", "pitching outs": "outs", "outs recorded": "outs"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=25))


def american_to_decimal(a):
    if a is None:
        return None
    a = float(a)
    return round(1 + (a / 100 if a > 0 else 100 / -a), 4)


def _stat_of(name):
    low = (name or "").lower()
    for k, v in STAT_KEYS.items():
        if k in low:
            return v
    return None


def _parse_market(m, pitcher, stat, start):
    """Yield rows from one FanDuel market (Over/Under or X+ alt)."""
    rows = []
    for r in m.get("runners") or []:
        rn = r.get("runnerName") or ""
        dec = american_to_decimal(((r.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {}).get("americanOdds"))
        if dec is None:
            continue
        hcap = r.get("handicap")
        if rn.lower().startswith("over") and hcap is not None:
            rows.append((pitcher, stat, float(hcap), "over", dec, start))
        elif rn.lower().startswith("under") and hcap is not None:
            rows.append((pitcher, stat, float(hcap), "under", dec, start))
        else:
            mm = re.search(r"(\d+)\+", rn)          # "5+ Strikeouts" => over 4.5
            if mm:
                rows.append((pitcher, stat, int(mm.group(1)) - 0.5, "over", dec, start))
    return rows


def collect():
    page = get(f"{BASE}/content-managed-page?page=CUSTOM&customPageId=mlb&timezone=America%2FNew_York&_ak={AK}")
    evs = page.get("attachments", {}).get("events", {})
    games = [(eid, e.get("name"), e.get("openDate")) for eid, e in evs.items() if "@" in (e.get("name") or "")]
    out, seen = [], set()
    for eid, name, start in games:
        try:
            ev = get(f"{BASE}/event-page?eventId={eid}&_ak={AK}&timezone=America%2FNew_York")
        except Exception:
            continue
        tabs = ev.get("layout", {}).get("tabs", {})
        titles = [(t.get("title") if isinstance(t, dict) else str(t)) for t in tabs.values()]
        prop_tabs = [t for t in titles if any(w in (t or "").lower() for w in ("popular", "player", "pitcher", "prop"))]
        for title in prop_tabs or ["Popular"]:
            slug = (title or "").lower().replace(" ", "-").replace("'", "")
            try:
                r = get(f"{BASE}/event-page?eventId={eid}&tab={slug}&_ak={AK}&timezone=America%2FNew_York")
            except Exception:
                continue
            for mid, m in (r.get("attachments", {}).get("markets", {}) or {}).items():
                nm = m.get("marketName") or ""
                stat = _stat_of(nm)
                if not stat or mid in seen:
                    continue
                seen.add(mid)
                pitcher = re.split(r"\s+-\s+", nm)[0].strip()
                out.extend(_parse_market(m, pitcher, stat, start))
    return out


def main():
    ts = dt.datetime.now().replace(microsecond=0).isoformat()
    recs = collect()
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS fanduel_props (
        collected_at TEXT, pitcher TEXT, stat TEXT, line REAL, side TEXT, odds REAL,
        start_time TEXT, PRIMARY KEY (collected_at, pitcher, stat, line, side))""")
    con.executemany("INSERT OR REPLACE INTO fanduel_props VALUES (?,?,?,?,?,?,?)",
                    [(ts, *r) for r in recs])
    con.commit()
    con.close()
    print(f"[{ts}] FanDuel {len(recs)} prop lines {dict(Counter(r[1] for r in recs))} "
          f"/ {len({r[0] for r in recs})} pitchers -> {DB}", flush=True)


if __name__ == "__main__":
    main()
