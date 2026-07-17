#!/usr/bin/env python3
# Exit 0 => "hot": a WNBA game is live, or tips within HOT_LEAD_MIN minutes (covers lineup lock).
# Exit 1 => "cold". Read by vm_loop.sh to switch wnba_watch to ~25s scratch polling near games.
# Fail-safe: on ANY error, exit 0 (prefer over-polling to missing a late scratch).
import sys, json, datetime, urllib.request

HOT_LEAD_MIN = 90      # go hot 1.5h before tip (user 2026-07-17: late rulings — Boston — need the fast probe BEFORE lineup lock; repriced lines repost in this window)
HOT_TRAIL_MIN = 200    # stay hot up to this long after tip if ESPN hasn't flipped state to "post"
URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"


def _hot():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(req, timeout=15))
    now = datetime.datetime.now(datetime.timezone.utc)
    for ev in data.get("events", []):
        state = ((ev.get("status") or {}).get("type") or {}).get("state", "")
        if state == "in":
            return True                       # a game is live -> hot
        if state == "pre":
            raw = ev.get("date", "")
            tip = None
            for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
                try:
                    tip = datetime.datetime.strptime(raw, fmt).replace(tzinfo=datetime.timezone.utc)
                    break
                except ValueError:
                    tip = None
            if tip is None:
                continue
            mins = (tip - now).total_seconds() / 60.0
            if -HOT_TRAIL_MIN <= mins <= HOT_LEAD_MIN:
                return True                   # tips soon (or just tipped) -> hot
    return False


if __name__ == "__main__":
    try:
        sys.exit(0 if _hot() else 1)
    except Exception:
        sys.exit(0)                           # fail-safe: over-poll rather than miss a scratch
