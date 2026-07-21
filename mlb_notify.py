"""MLB outs-under MODEL notifications — same ntfy format as the WNBA pushes.

Fires ONE concise push per NEW model play (away+contact + premium: low-patience opp OR line >
recent outs). Deduped via mlb_notified.json so a play never re-pings across 30-min cycles.

Runs in GitHub Actions (collect-odds.yml), NOT the VM — MLB is not speed-sensitive (season-stat
model, props post hours ahead) and Actions is more reliable than the swap-thrashing Oracle VM.
No NTFY_TOPIC (env) -> silent no-op (stays benched).
"""
import datetime as dt
import json
import os
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SEEN = HERE / "mlb_notified.json"

# team full-name -> short nickname for the banner (last word is the recognizable bit)
_NICK = {"Diamondbacks": "DBacks", "Athletics": "As"}


def _nick(team):
    last = (team or "").split()[-1] if team else "?"
    return _NICK.get(last, last)


def main():
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("mlb_notify: no NTFY_TOPIC -> benched (no push)")
        return
    try:
        import dashboard as D
    except Exception as e:                                 # never let a bad import break the run
        print(f"mlb_notify: dashboard import failed: {str(e)[:100]}")
        return
    plays = D._mlb_plays()                                 # today's MODEL plays (premium only)
    if not plays:
        print("mlb_notify: no model plays")
        return
    try:
        seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    except (ValueError, OSError):
        seen = set()
    today = dt.date.today().isoformat()
    sent = 0
    for p in plays:
        key = f"{today}|{p['pitcher']}|{p['line']:g}"
        if key in seen:
            continue
        loc = f"@{_nick(p['opp'])}" if p.get("away") else _nick(p.get("opp"))
        # BODY-only, WNBA-style: 🚨 {opp} {pitcher} OUTS u{line} {odds} {book} ★★
        text = (f"🚨 ⚾ {loc} {D._short(p['pitcher'])} OUTS u{p['line']:g} "
                f"{D._am(p['odds'])} {p['book'].upper()} ★★")
        try:
            r = requests.post(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                              params={"title": "Pickz", "priority": "high"}, timeout=15)
            r.raise_for_status()
            seen.add(key)
            sent += 1
            print(f"pushed: {text}")
        except requests.RequestException as e:
            print(f"mlb_notify push failed: {str(e)[:80]}")
    try:
        SEEN.write_text(json.dumps(sorted(seen)[-800:]))  # cap so the file can't grow forever
    except OSError:
        pass
    print(f"mlb_notify: sent {sent} new / {len(plays)} model plays")


if __name__ == "__main__":
    main()
