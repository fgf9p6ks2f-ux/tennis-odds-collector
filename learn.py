"""Soft-spot learner — the honest "gets better with results" loop (runs daily).

The projection models can't beat a sharp book; the edge is finding FanDuel markets that
are softer than Pinnacle. This learns WHICH markets those are, from realized results, and
adapts:
  * Reads the bet ledger's per-(sport, stat) realized CLV, ROI and W-L.
  * BENCHES a market once it has a real sample (>= MIN_N closed bets) whose average CLV is
    negative — i.e. we're consistently getting WORSE than the sharp close there, so it
    isn't soft for us. bet_ledger.flag() then stops betting benched markets.
  * Keeps (green-lights) markets with positive CLV — even if their W-L is unlucky.
Writes bet_filters.json (consumed by the ledger) + learning_report.md.

CLV, not W-L, is the teacher: it's the leading indicator of a real edge and needs far
fewer samples to trust. This is the legitimate form of "self-improvement" here — no model
magically starts beating the books; the system learns where to point.
"""
import datetime as dt
import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "bet_ledger.sqlite"
FILTERS = HERE / "bet_filters.json"
REPORT = HERE / "learning_report.md"

MIN_N = 40           # need this many CLV-measured bets before benching a market
BENCH_CLV = -1.0     # bench if average CLV <= this (%) over that sample


def buckets():
    if not LEDGER.exists():
        return {}
    con = sqlite3.connect(LEDGER)
    try:
        rows = con.execute(
            "SELECT sport, stat, clv_pct, result, pnl_units FROM bets").fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    agg = {}
    for sp, st, clv, res, pnl in rows:
        m = agg.setdefault((sp, st), {"clv": [], "w": 0, "l": 0, "pnl": 0.0, "settled": 0})
        if clv is not None:
            m["clv"].append(clv)
        if res in ("W", "L"):
            m["settled"] += 1
            m["pnl"] += pnl or 0
            m[res.lower()] += 1
    return agg


def main():
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    agg = buckets()
    benched, table = [], []
    for (sp, st), m in sorted(agg.items()):
        n_clv = len(m["clv"])
        avg_clv = sum(m["clv"]) / n_clv if n_clv else None
        roi = (m["pnl"] / m["settled"] * 100) if m["settled"] else None
        bench = n_clv >= MIN_N and avg_clv is not None and avg_clv <= BENCH_CLV
        if bench:
            benched.append([sp, st])
        table.append((sp, st, n_clv, avg_clv, m["w"], m["l"], roi, bench))

    FILTERS.write_text(json.dumps(
        {"generated": ts, "min_n": MIN_N, "bench_clv": BENCH_CLV, "benched": benched}, indent=2))

    lines = ["# Soft-spot learning report", "", f"_{ts}_", "",
             f"Benches a (sport, stat) market once it has ≥{MIN_N} CLV-measured bets whose "
             f"average CLV ≤ {BENCH_CLV:.0f}%. The ledger then stops betting it. CLV is the "
             "teacher — negative CLV over a real sample means the market isn't soft for us.", ""]
    if not table:
        lines += ["No settled bets yet — everything green-lit until results accrue "
                  "(needs weeks of live slates before any market has a trustworthy sample).", ""]
    else:
        lines += ["| sport | stat | CLV bets | avg CLV | W-L | ROI | status |",
                  "|---|---|---|---|---|---|---|"]
        for sp, st, n, clv, w, l, roi, bench in table:
            lines.append(
                f"| {sp} | {st} | {n} | {('%+.2f%%' % clv) if clv is not None else '—'} | "
                f"{w}-{l} | {('%+.1f%%' % roi) if roi is not None else '—'} | "
                f"{'🛑 benched' if bench else ('✅ green' if n >= MIN_N else '⏳ learning')} |")
        lines += ["", f"**Benched markets:** {benched or 'none'}", ""]
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
