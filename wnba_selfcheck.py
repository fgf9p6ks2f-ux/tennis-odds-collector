#!/usr/bin/env python3
"""Once-a-day WNBA loop self-check — pings ntfy ONLY if something is broken; silent when healthy.

Catches the SILENT failure modes (the loud ones — flag-engine crash — already page from the loop):
  1. loop service not active
  2. loop STALLED (service 'active' but no artifact written recently — a hung cycle)
  3. injury feed stale / empty / unreadable (scanning has quietly stopped)
  4. guard-rebound gate LEAK (a guard reb <=3.5 firm flag slipped through)

Run by cron ~1pm MT (19:00 UTC), a few hours before games, so a break is caught with time to fix.

    python3 wnba_selfcheck.py            # run checks; ntfy + exit 1 on any failure
    python3 wnba_selfcheck.py --test     # force a test ping to verify the ntfy plumbing
"""
import os, sys, time, json, sqlite3, subprocess, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = "wnba-loop.service"
ENV_FILE = Path("/home/ubuntu/wnba-loop.env")


def ntfy_topic():
    t = os.environ.get("NTFY_TOPIC")
    if t:
        return t
    if ENV_FILE.exists():
        for ln in ENV_FILE.read_text().splitlines():
            ln = ln.strip()
            if ln.startswith("export "):
                ln = ln[7:]
            if ln.startswith("NTFY_TOPIC"):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def push(topic, title, body):
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic, data=body.encode(), method="POST",
        headers={"Title": title, "Priority": "urgent", "Tags": "warning"})
    urllib.request.urlopen(req, timeout=10)


def _age_min(p):
    return (time.time() - p.stat().st_mtime) / 60 if p.exists() else 1e9


def check():
    fails = []
    # 1) service active
    try:
        s = subprocess.run(["systemctl", "is-active", SERVICE],
                           capture_output=True, text=True, timeout=10).stdout.strip()
        if s != "active":
            fails.append("loop service is '%s' (not active)" % s)
    except Exception as e:
        fails.append("could not query service: %s" % e)
    # 2) loop cycling — the freshest loop-written artifact should be minutes old
    arts = [HERE / "docs/index.html", HERE / "wnba_ledger.sqlite", HERE / "wnba_injury_report_cache.json"]
    freshest = min((_age_min(a) for a in arts), default=1e9)
    if freshest > 30:
        fails.append("loop looks STALLED — newest artifact %.0f min old (>30)" % freshest)
    # 3) injury feed fresh + parseable + non-empty
    inj = HERE / "wnba_injury_report_cache.json"
    if _age_min(inj) > 45:
        fails.append("injury cache stale (%.0f min old) — scanning may have stopped" % _age_min(inj))
    else:
        try:
            d = json.loads(inj.read_text())
            if not d.get("rows"):
                fails.append("injury cache parsed but has 0 rows")
        except Exception as e:
            fails.append("injury cache unreadable: %s" % e)
    # 4) guard-rebound gate leak — any guard rebound <=3.5 firm flag in the last day
    try:
        lc = sqlite3.connect(HERE / "wnba_ledger.sqlite")
        lc.row_factory = sqlite3.Row
        pl = sqlite3.connect(HERE / "wnba_proj_log.sqlite")
        pos = {r[0]: r[1] for r in pl.execute(
            "SELECT DISTINCT player,pos FROM projections WHERE pos IS NOT NULL")}
        rows = lc.execute(
            "SELECT player,line FROM predictions WHERE stat='rebounds' "
            "AND (side IS NULL OR side='over') AND line<=3.5 "
            "AND pred_date>=date('now','-1 day')").fetchall()
        leaks = [r["player"] for r in rows if (pos.get(r["player"]) or "").upper().startswith("G")]
        if leaks:
            fails.append("GUARD-REBOUND GATE LEAK: " + ", ".join(sorted(set(leaks))))
    except Exception as e:
        fails.append("guard-reb leak check errored: %s" % e)
    return fails


def main():
    topic = ntfy_topic()
    if "--test" in sys.argv:
        if topic:
            push(topic, "WNBA self-check TEST", "plumbing OK — this is a test ping (ignore)")
            print("test ping sent to topic:", topic)
        else:
            print("NO NTFY TOPIC FOUND")
        return
    fails = check()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if fails:
        body = "\n".join("- " + f for f in fails)
        print("[%s] SELF-CHECK FAILED:\n%s" % (stamp, body))
        if topic:
            try:
                push(topic, "WNBA self-check FAILED", body)
            except Exception as e:
                print("ntfy send failed:", e)
        sys.exit(1)
    print("[%s] self-check OK — loop active, scanning, gate holding" % stamp)


if __name__ == "__main__":
    main()
