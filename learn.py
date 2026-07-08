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
MIN_N_ROI = 60       # ROI-fallback sample: when CLV coverage is thin (closing line was
BENCH_ROI = -5.0     # rarely capturable) bench on realized ROI <= this (%) instead


def buckets():
    """Aggregate per (sport, stat, src) — src matters: the same stat can be soft when
    compared DIRECTLY line-for-line vs Pinnacle yet a loser when model-priced."""
    if not LEDGER.exists():
        return {}
    con = sqlite3.connect(LEDGER)
    try:
        rows = con.execute(
            "SELECT sport, stat, src, clv_pct, result, pnl_units FROM bets").fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    agg = {}
    for sp, st, src, clv, res, pnl in rows:
        m = agg.setdefault((sp, st, src or "direct"),
                           {"clv": [], "w": 0, "l": 0, "pnl": 0.0, "settled": 0})
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
    for (sp, st, src), m in sorted(agg.items()):
        n_clv = len(m["clv"])
        avg_clv = sum(m["clv"]) / n_clv if n_clv else None
        roi = (m["pnl"] / m["settled"] * 100) if m["settled"] else None
        bench_clv = n_clv >= MIN_N and avg_clv is not None and avg_clv <= BENCH_CLV
        # CLV is only a trustworthy teacher for DIRECT prop buckets (measured vs
        # Pinnacle's own posted line). It LIES for model buckets (self-referencing) and
        # is ABSENT for h2h/totals buckets (no closing snapshot in this DB). For any
        # bucket where CLV can't be trusted — model/h2h, or CLV covers < half the
        # settled bets — fall back to realized ROI.
        clv_thin = n_clv < m["settled"] * 0.5
        bench_roi = (src in ("model", "h2h") or clv_thin) and m["settled"] >= MIN_N_ROI \
            and roi is not None and roi <= BENCH_ROI
        bench = bench_clv or bench_roi
        if bench:
            benched.append([sp, st, src])
        table.append((sp, st, src, n_clv, avg_clv, m["w"], m["l"], roi, bench))

    FILTERS.write_text(json.dumps(
        {"generated": ts, "min_n": MIN_N, "bench_clv": BENCH_CLV, "benched": benched}, indent=2))

    lines = ["# Soft-spot learning report", "", f"_{ts}_", "",
             f"Benches a (sport, stat, src) market once it has ≥{MIN_N} CLV-measured bets "
             f"whose average CLV ≤ {BENCH_CLV:.0f}% — or, when CLV coverage is thin, "
             f"≥{MIN_N_ROI} settled bets at ROI ≤ {BENCH_ROI:.0f}%. The ledger then stops "
             "betting it. CLV is the teacher; realized ROI is the backstop.", ""]
    if not table:
        lines += ["No settled bets yet — everything green-lit until results accrue "
                  "(needs weeks of live slates before any market has a trustworthy sample).", ""]
    else:
        lines += ["| sport | stat | src | CLV bets | avg CLV | W-L | ROI | status |",
                  "|---|---|---|---|---|---|---|---|"]
        for sp, st, src, n, clv, w, l, roi, bench in table:
            lines.append(
                f"| {sp} | {st} | {src} | {n} | "
                f"{('%+.2f%%' % clv) if clv is not None else '—'} | "
                f"{w}-{l} | {('%+.1f%%' % roi) if roi is not None else '—'} | "
                f"{'🛑 benched' if bench else ('✅ green' if n >= MIN_N else '⏳ learning')} |")
        lines += ["", f"**Benched markets:** {benched or 'none'}", ""]
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
