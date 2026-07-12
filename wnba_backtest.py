"""2-day WNBA backtest — the first replay at ACTUAL posted lines.

We have fd_lines (FanDuel/DK prop lines, timestamped) for 2026-07-07..08 AND the box
scores. So for the injury spots on those dates we can replay the model LEAK-FREE:
  - detect who sat (key player, team played that day, no game log row that day),
  - project beneficiaries from games strictly BEFORE that day,
  - look up the line the book ACTUALLY posted that day (fd_lines),
  - flag with the live logic (prop_edges), then grade vs what actually happened.

Tiny sample (2 days) => read as a smoke test / directional signal, NOT proof. It tests
the whole chain end to end on real lines, which nothing else has.

    python wnba_backtest.py
"""
from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

import requests

import wnba_tonight as T
import wnba_wowy as W

DATES = ["2026-07-07", "2026-07-08"]
FD_DB = Path(__file__).resolve().parent / "fanduel_props.sqlite"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"


def teams_on(date_iso):
    """Set of team abbrevs that played on a given ET date."""
    d = date_iso.replace("-", "")
    j = requests.get(f"{SITE}/scoreboard?dates={d}", headers=H, timeout=20).json()
    out = set()
    for e in j.get("events", []):
        for c in e.get("competitions", [{}])[0].get("competitors", []):
            out.add(c["team"]["abbreviation"])
    return out


def historical_props(player, date_iso):
    """{stat: {line: (best_over_dec, best_under_dec)}} the books ACTUALLY posted for player on date —
    mirrors posted_props' both-sides structure so prop_edges can test the UNDER side (the validated
    edge), not just overs. (Was over-only single-value, which crashed the current prop_edges.)"""
    if not FD_DB.exists():
        return {}
    con = sqlite3.connect(FD_DB)
    rows = con.execute(
        "SELECT stat, line, side, odds FROM fd_lines WHERE sport='wnba' AND player=? "
        "AND line IS NOT NULL AND substr(collected_at,1,10)=?",
        (player, date_iso)).fetchall()
    con.close()
    best = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for stat, line, side, odds in rows:
        if stat in T.PROP_STATS and side in ("over", "under"):
            k = round(float(line), 1)
            best[stat][k][0 if side == "over" else 1] = max(
                best[stat][k][0 if side == "over" else 1], float(odds))
    return {s: {k: tuple(v) for k, v in d.items()} for s, d in best.items()}


def run():
    pl = W.players()                                  # season avgs: used only to pick "key" players
    logs = {}                                         # cache full logs

    def log(pid):
        if pid not in logs:
            try:
                logs[pid] = W.game_log(pid)
            except RuntimeError:
                logs[pid] = []
        return logs[pid]

    graded = []                                       # (date, out, benef, stat, line, dec, ev, actual, win)
    for D in DATES:
        playing = teams_on(D)
        if not playing:
            continue
        # key players who sat: team played D, >=20 mpg, but no game log row on D
        for name, p in pl.items():
            if p["team"] not in playing or p["min"] < 20:
                continue
            plog = log(p["id"])
            if any(g["date"][:10] == D for g in plog):
                continue                              # they played -> not out
            tlog_before = [g for g in plog if g["date"][:10] < D]
            if len(tlog_before) < 3:
                continue                              # not enough history to be a real "regular"
            # beneficiaries on that team
            for bname, bv in pl.items():
                if bv["team"] != p["team"] or bname == name or bv["gp"] < 5:
                    continue
                blog = log(bv["id"])
                before = [g for g in blog if g["date"][:10] < D]
                today = [g for g in blog if g["date"][:10] == D]
                if not today or len(before) < 4:
                    continue                          # no result to grade, or too little history
                w = W.wowy(before, tlog_before)
                if w["n_without"] < 2 or w["without"]["min"]["mean"] <= w["with"]["min"]["mean"]:
                    continue                          # not a genuine beneficiary
                proj = w["without"]["min"]["mean"]
                # flag with the LIVE logic but the HISTORICAL posted lines
                T.posted_props = lambda pp, _d=D: historical_props(pp, _d)   # noqa: E731
                vac = {"points": p["pts"], "rebounds": p["reb"], "assists": p["ast"]}
                for e in T.prop_edges(bname, before, proj, w, vac):
                    actual = today[0][T.PROP_STATS[e["stat"]]]
                    win = actual > e["line"]
                    graded.append((D, name, bname, e["stat"], e["line"], e["dec"],
                                   e["ev"], actual, win))
    return graded


def _tally(rs, label):
    if not rs:
        print(f"  {label}: (none)")
        return
    w = sum(1 for r in rs if r[8])
    u = sum((r[5] - 1) if r[8] else -1 for r in rs)
    print(f"  {label}: {w}-{len(rs)-w}  {u:+.1f}u  win {w/len(rs)*100:.0f}%  ROI {u/len(rs)*100:+.0f}%")


def main():
    g = run()
    print(f"WNBA 2-day backtest ({DATES[0]}..{DATES[-1]}) — replay at real posted lines\n")
    if not g:
        print("no gradeable injury spots in the window. Nothing to conclude — expected for 2 days.")
        return
    # dedup: you'd bet each (day, player, stat, line) ONCE, no matter how many teammates
    # were out. Keep the highest-EV attribution.
    seen, spots = {}, []
    for r in sorted(g, key=lambda r: -r[6]):
        k = (r[0], r[2], r[3], r[4])
        if k not in seen:
            seen[k] = 1
            spots.append(r)
    _tally(spots, f"ALL {len(spots)} spots")
    print()
    by = defaultdict(list)
    for r in spots:
        by[r[3]].append(r)
    for stat, rs in sorted(by.items()):
        _tally(rs, f"{stat:9}")
    print()
    # the tell from the raw run: the FATTEST EV flags are thin-sample projections that bust.
    _tally([r for r in spots if r[6] > 0.40], "fat-EV (>40%, thin-sample flags)")
    _tally([r for r in spots if 0.05 <= r[6] <= 0.30], "grounded (5-30% EV)")
    print("\n  spot detail:")
    for D, out, ben, stat, line, dec, ev, actual, win in sorted(g, key=lambda r: -r[6])[:20]:
        am = f"+{round((dec-1)*100)}" if dec >= 2 else f"{round(-100/(dec-1))}"
        print(f"   {D} {out[:14]:14} OUT -> {ben[:16]:16} {stat[:3]} o{line:g} @{am:>5} "
              f"(+{ev*100:.0f}%EV) -> {actual:g} {'WIN' if win else 'loss'}")


if __name__ == "__main__":
    main()
