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
# Tracker reset (2026-07-08): earlier results were polluted by since-fixed bugs
# (mislabeled FD markets, dead grading, the benched TB model). The digest counts only
# bets PLACED after this epoch; the learner still trains on full history.
EPOCH = "2026-07-08T20:00:00"
NAMES = {"tennis": "TENNIS", "mlb": "BASEBALL", "wnba": "BASKETBALL (WNBA)",
         "nba": "BASKETBALL (NBA)", "nfl": "NFL",
         "esoccer": "ESOCCER", "ebasketball": "EBASKETBALL", "efootball": "EFOOTBALL"}


def mt_day_utc_window(target_date):
    """(start_utc_iso, end_utc_iso, mt_date) for a specific MT calendar date."""
    day0 = dt.datetime.combine(target_date, dt.time(0, 0), tzinfo=MT)
    day1 = day0 + dt.timedelta(days=1)
    f = lambda t: t.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat()
    return f(day0), f(day1), target_date.isoformat()


def fmt_u(u):
    return f"{u:+.2f}u (${u * UNIT:+,.0f})"


def build(target_date):
    lo, hi, mt_date = mt_day_utc_window(target_date)
    con = sqlite3.connect(LEDGER)
    q = con.execute

    def agg(rows):
        w = sum(1 for r in rows if r[0] == "W"); l = sum(1 for r in rows if r[0] == "L")
        pnl = sum(r[1] or 0 for r in rows)
        return w, l, pnl

    day = q("SELECT sport, result, pnl_units FROM bets WHERE graded_at>=? AND graded_at<? "
            "AND placed_at>=?", (lo, hi, EPOCH)).fetchall()
    alltime = q("SELECT sport, result, pnl_units FROM bets WHERE result IS NOT NULL "
                "AND placed_at>=?", (EPOCH,)).fetchall()
    sports = sorted({r[0] for r in alltime} | {r[0] for r in day})

    lines = [f"Daily digest - {mt_date} (MT)"]
    w, l, pnl = agg([(r[1], r[2]) for r in day])
    lines.append(f"TODAY: {w}-{l}  {fmt_u(pnl)}" if day else "TODAY: no bets settled")
    aw, al, apnl = agg([(r[1], r[2]) for r in alltime])
    lines.append(f"ALL-TIME (since 7/8 reset): {aw}-{al}  {fmt_u(apnl)}")
    lines.append("")
    for sp in sports:
        dsp = [(r[1], r[2]) for r in day if r[0] == sp]
        asp = [(r[1], r[2]) for r in alltime if r[0] == sp]
        dw, dl, dp = agg(dsp); tw, tl, tp = agg(asp)
        nm = NAMES.get(sp, sp.upper())
        daypart = f"today {dw}-{dl} {dp:+.2f}u · " if dsp else "today - · "
        lines.append(f"{nm}: {daypart}all-time {tw}-{tl} {fmt_u(tp)}")

    # CLV of bets that started today (leading indicator)
    c = q("SELECT sport, clv_pct FROM bets WHERE clv_pct IS NOT NULL "
          "AND start_time>=? AND start_time<? AND placed_at>=?", (lo, hi, EPOCH)).fetchall()
    if c:
        lines.append("")
        lines.append(f"CLV today: {sum(r[1] for r in c)/len(c):+.2f}% avg ({len(c)} closed)")
    pend = q("SELECT COUNT(*) FROM bets WHERE result IS NULL AND start_time<? "
             "AND placed_at>=?", (hi, EPOCH)).fetchone()[0]
    if pend:
        lines.append(f"Pending: {pend} (settle tomorrow)")
    con.close()
    return "\n".join(lines), mt_date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="skip the 11:40pm-MT clock guard")
    args = ap.parse_args()
    now_mt = dt.datetime.now(dt.timezone.utc).astimezone(MT)
    # Pick the MT day to summarize by wall-clock, tolerant of GitHub's flaky cron (it can
    # fire hours late). Normal fire ~23:40 MT -> summarize today. A run delayed past
    # midnight (now rolled to the next day) -> summarize the day that just ENDED, not the
    # empty new one. This gives a ~14h acceptance window (evening through next midday);
    # the day-header dedupe makes overlapping/extra fires idempotent (exactly one send).
    if args.force:
        target = now_mt.date()
    elif now_mt.hour >= 20:                        # 20:00-23:59 MT: today is ending
        target = now_mt.date()
    elif now_mt.hour < 14:                         # 00:00-13:59 MT: delayed -> yesterday
        target = (now_mt - dt.timedelta(days=1)).date()
    else:                                          # mid-afternoon: not a digest window
        print(f"not digest time in MT (now {now_mt:%H:%M}) — exiting")
        return
    body, mt_date = build(target)
    if not args.force and LOG.exists() and f"## {mt_date}" in LOG.read_text():
        print(f"digest for {mt_date} already sent — exiting (dedupe)")
        return
    print(body)
    # forced/manual runs get a distinct header so they never trip the dedupe that
    # protects the real nightly send
    hdr = f"## {mt_date} (forced)" if args.force else f"## {mt_date}"
    with open(LOG, "a") as f:
        f.write(f"\n{hdr}\n\n```\n{body}\n```\n")
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
