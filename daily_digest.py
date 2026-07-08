"""11:59pm Mountain Time daily digest — the day's betting results, pushed to the phone.

Covers the +EV ledger sports (tennis / MLB / WNBA). Table tennis is intentionally NOT
included (it lives in the tt-elite repo and has no odds feed for CLV).

For "today" (the Mountain-Time calendar day):
  - record of bets GRADED today: W-L-push, ±units, $ P&L at $100/unit
  - average CLV of bets that CLOSED today (line-movement truth; the leading indicator)
  - what's still pending (started but not yet graded — late games settle tomorrow)
  - cumulative all-time record / units / CLV for context

Runs from GitHub Actions at 05:40 & 06:40 UTC; the MT-clock guard makes exactly one of
those fire at ~11:40pm MT year-round (DST-proof). --force skips the guard for testing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    MT = ZoneInfo("America/Denver")
except Exception:
    MT = dt.timezone(dt.timedelta(hours=-6))

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "bet_ledger.sqlite"
LOG = HERE / "digests.md"
UNIT = 100.0


def mt_day_utc_window(now_utc):
    """(start_utc_iso, end_utc_iso, mt_date) for the current MT calendar day."""
    now_mt = now_utc.astimezone(MT)
    day0 = now_mt.replace(hour=0, minute=0, second=0, microsecond=0)
    day1 = day0 + dt.timedelta(days=1)
    f = lambda t: t.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat()
    return f(day0), f(day1), now_mt.date().isoformat()


def fmt_u(u):
    return f"{u:+.2f}u (${u * UNIT:+,.0f})"


def build(now_utc):
    lo, hi, mt_date = mt_day_utc_window(now_utc)
    con = sqlite3.connect(LEDGER)
    q = con.execute
    lines = [f"Daily digest — {mt_date} (MT)"]

    # --- today: graded bets ---
    g = q("SELECT sport, result, pnl_units FROM bets WHERE graded_at >= ? AND graded_at < ?",
          (lo, hi)).fetchall()
    if g:
        w = sum(1 for r in g if r[1] == "W")
        l = sum(1 for r in g if r[1] == "L")
        p = sum(1 for r in g if r[1] == "push")
        pnl = sum(r[2] or 0 for r in g)
        lines.append(f"Today: {w}-{l}" + (f"-{p}" if p else "") + f"  {fmt_u(pnl)}")
        for sp in ("tennis", "mlb", "wnba"):
            gs = [r for r in g if r[0] == sp]
            if gs:
                ws = sum(1 for r in gs if r[1] == "W")
                ls = sum(1 for r in gs if r[1] == "L")
                pn = sum(r[2] or 0 for r in gs)
                lines.append(f"  {sp.upper()}: {ws}-{ls}  {fmt_u(pn)}")
    else:
        lines.append("Today: no bets settled")

    # --- today: CLV of bets that started today (closing line captured at start) ---
    c = q("SELECT sport, clv_pct FROM bets WHERE clv_pct IS NOT NULL "
          "AND start_time >= ? AND start_time < ?", (lo, hi)).fetchall()
    if c:
        avg = sum(r[1] for r in c) / len(c)
        lines.append(f"CLV today: {avg:+.2f}% avg over {len(c)} closed bets")
        for sp in ("tennis", "mlb", "wnba"):
            cs = [r[1] for r in c if r[0] == sp]
            if cs:
                lines.append(f"  {sp.upper()}: {sum(cs)/len(cs):+.2f}% ({len(cs)})")
    else:
        lines.append("CLV today: no bets closed")

    # --- pending ---
    pend = q("SELECT COUNT(*) FROM bets WHERE result IS NULL AND start_time < ?",
             (hi,)).fetchone()[0]
    if pend:
        lines.append(f"Pending: {pend} bet(s) not yet graded (late games settle tomorrow)")

    # --- cumulative ---
    allg = q("SELECT result, pnl_units FROM bets WHERE result IS NOT NULL").fetchall()
    if allg:
        w = sum(1 for r in allg if r[0] == "W")
        l = sum(1 for r in allg if r[0] == "L")
        pnl = sum(r[1] or 0 for r in allg)
        clvs = [r[0] for r in q("SELECT clv_pct FROM bets WHERE clv_pct IS NOT NULL").fetchall()]
        clv = f", CLV {sum(clvs)/len(clvs):+.2f}% ({len(clvs)})" if clvs else ""
        lines.append(f"All-time: {w}-{l}  {fmt_u(pnl)}{clv}")
    con.close()
    return "\n".join(lines), mt_date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="skip the 11:40pm-MT clock guard")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    now_mt = now.astimezone(MT)
    if not args.force and not (now_mt.hour == 23 and now_mt.minute >= 25):
        print(f"not digest time in MT ({now_mt:%H:%M}) — exiting (other cron will fire)")
        return
    body, mt_date = build(now)
    print(body)
    with open(LOG, "a") as f:
        f.write(f"\n## {mt_date}\n\n```\n{body}\n```\n")
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers={"Title": "Daily betting digest", "Tags": "bar_chart"},
                          timeout=15)
            print("pushed to ntfy")
        except requests.RequestException as e:
            print("ntfy push failed:", e)


if __name__ == "__main__":
    main()
