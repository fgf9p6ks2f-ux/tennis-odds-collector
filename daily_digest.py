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
WNBA_LEDGER = HERE / "wnba_ledger.sqlite"          # the injury-driven autobetter (the focus)
LOG = HERE / "digests.md"
UNIT = 100.0
# Fresh-start reset (2026-07-09): models are debugged and the operation is now focused on
# table tennis + WNBA (MLB / tennis / esports benched). The record counts from this epoch;
# all underlying data is preserved. TT has its own digest in the tt-elite repo.
EPOCH = "2026-07-09T06:00:00"                       # 00:00 MT, 2026-07-09
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


def _wnba_autobetter(target_date):
    """Record from the injury-driven WNBA prop ledger (wnba_ledger.sqlite) — the focus.
    Flat 1u at the taken price; W = the over hit. Returns (lines, has_data)."""
    if not WNBA_LEDGER.exists():
        return (["WNBA AUTOBETTER: ledger initializing"], False)
    con = sqlite3.connect(WNBA_LEDGER)
    rows = con.execute("SELECT pred_date, result, odds, side FROM predictions "
                       "WHERE graded=1 AND pred_date>=?", (EPOCH[:10],)).fetchall()
    pend = con.execute("SELECT COUNT(*) FROM predictions WHERE graded=0 AND pred_date>=?",
                       (EPOCH[:10],)).fetchone()[0]
    con.close()

    def rec(rs):
        # win = result == the side we bet (the model bets unders too since the pivot;
        # counting 'over hit' as the win was flipping the record on every under).
        dec = [r for r in rs if r[1] in ("over", "under")]   # decided (excludes push)
        w = sum(1 for r in dec if r[1] == r[3])
        u = sum((r[2] - 1) if r[1] == r[3] else -1 for r in dec)
        return w, len(dec) - w, u

    today = [r for r in rows if r[0] == target_date]
    tw, tl, tu = rec(today)
    aw, al, au = rec(rows)
    lines = ["WNBA AUTOBETTER (injury props):"]
    lines.append(f"TODAY: {tw}-{tl}  {fmt_u(tu)}" if today else "TODAY: no bets graded yet")
    lines.append(f"ALL-TIME (since 7/9): {aw}-{al}  {fmt_u(au)}")
    if pend:
        lines.append(f"Pending: {pend} (grade after games settle)")
    return (lines, bool(rows or pend))


def _watch_health():
    """HEARTBEAT for the opening-line loop. wnba-watch self-redispatches every ~5h, so if its
    most recent run STARTED more than 6h ago the immortal chain has broken and opener alerts are
    OFF — exactly the silent failure that cost 4 days. Checks the Actions API (a true liveness
    signal, independent of whether games are on). Returns a ⚠ line if down, '' if healthy. Never
    raises — a digest must still send even if this check fails."""
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "fgf9p6ks2f-ux/tennis-odds-collector")
    if not tok:
        return ""
    try:
        import requests
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/wnba-watch.yml/runs",
                         params={"per_page": 1}, timeout=15,
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
        runs = r.json().get("workflow_runs", [])
        if not runs:
            return "⚠️ OPENER LOOP DOWN — no wnba-watch runs found. Opening-line alerts are OFF."
        age_h = (dt.datetime.now(dt.timezone.utc)
                 - dt.datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))).total_seconds() / 3600
        if age_h > 6:
            return (f"⚠️ OPENER LOOP DOWN — wnba-watch last started {age_h:.0f}h ago; opening-line "
                    f"alerts are OFF. Re-dispatch it (gh workflow run wnba-watch.yml).")
        return ""
    except Exception:
        return ""


def build(target_date):
    _, _, mt_date = mt_day_utc_window(target_date)
    lines = [f"Daily digest - {mt_date} (MT)", ""]
    hb = _watch_health()                      # prominent at the top so a dead loop can't be missed
    if hb:
        lines += [hb, ""]
    wnba_lines, _ = _wnba_autobetter(mt_date)
    lines += wnba_lines
    try:                                     # OOS calibration trend — silent until >=6 slates
        import wnba_oos_backtest as OOS
        oos = OOS.digest_lines(WNBA_LEDGER)
        if oos:
            lines.append("")
            lines += oos
    except Exception:
        pass
    lines.append("")
    lines.append("Focus: TT + WNBA. MLB / tennis / esports benched (data kept). "
                 "Table tennis has its own nightly digest.")
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
