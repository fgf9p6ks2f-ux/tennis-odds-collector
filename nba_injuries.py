"""NBA port · P1 — injury feed (ESPN), the fast layer.

Mirrors the WNBA system's proven feed (same endpoint family that caught 38/38 outs):
ESPN's NBA injuries API -> nba_injuries.json {player: {status, team, detail, date}}.
Runs every collect-odds cycle; in the offseason it's stale season-enders (harmless) —
the point is the plumbing is live and tested long before opening night.

P2 ADD-ON (October, when reports resume): the OFFICIAL NBA injury report — published on
a fixed clock (5:30pm ET + hourly near tip) as a structured PDF. That's the scheduled-
edge-window upgrade over anything WNBA had; parse it when live PDFs exist to test against.

    python nba_injuries.py           # fetch + write nba_injuries.json + print summary
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / "nba_injuries.json"
URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch():
    j = requests.get(URL, headers=UA, timeout=25).json()
    out = {}
    for team in j.get("injuries", []):
        tname = (team.get("team") or {}).get("abbreviation") or ""
        for inj in team.get("injuries", []):
            ath = inj.get("athlete") or {}
            name = ath.get("displayName") or ""
            if not name:
                continue
            out[name] = {
                "team": tname,
                "status": (inj.get("status") or ""),                  # Out / Day-To-Day / ...
                "detail": ((inj.get("details") or {}).get("type") or ""),
                "date": (inj.get("date") or "")[:10],
            }
    return out


def main():
    inj = fetch()
    OUT.write_text(json.dumps(
        {"fetched_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
         "injuries": inj}, indent=1))
    from collections import Counter
    by = Counter(v["status"] for v in inj.values())
    print(f"nba injuries: {len(inj)} players {dict(by)} -> {OUT.name}")


if __name__ == "__main__":
    main()
