"""GG esports line-timing analytics — is it better to bet early or at T-5min?

Every collect cycle snapshots FanDuel's esports lines into gg_quotes. For each match
we can watch the total move from first-posted (~30-60 min out) to its last pre-start
quote (the 'close'). This answers a money question: our flag side is fixed at flag
time — does the line DRIFT toward our side (so betting early captures a better number)
or away (so waiting is better)? Reported per sport in gg_timing.md.

  drift = (close_line − open_line), signed toward OUR side:
    · under flags: a RISING line helps us (we grabbed the lower number early) → bet early
    · over  flags: a FALLING line helps us → bet early
  net "early edge" = average signed line improvement, in points, from betting at open.

Also reports odds drift (did the price on our side get worse = sharper money on it).
Pure analytics — writes gg_timing.md, changes no bets.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
GG_DB = HERE / "gg.sqlite"
LEDGER = HERE / "bet_ledger.sqlite"
REPORT = HERE / "gg_timing.md"

# plausible MATCH total band per sport — excludes early gg_quotes rows captured from
# quarter markets before the exact-market-name fix (a match line can't be ~28 in nba)
BAND = {"fifa": (1.5, 9.5), "nba": (90.0, 175.0), "nfl": (20.0, 75.0)}


def _flag_sides():
    """{(sport, p1, p2, start): side} — the side we actually flagged, from the ledger."""
    if not LEDGER.exists():
        return {}
    con = sqlite3.connect(LEDGER)
    out = {}
    try:
        for sp, player, side, start in con.execute(
                "SELECT sport, player, side, start_time FROM bets WHERE src='h2h'"):
            nicks = tuple(sorted(n.strip().upper() for n in str(player).split(" v ")))
            out[(sp, nicks, str(start)[:16])] = side
    except sqlite3.OperationalError:
        pass
    con.close()
    return out


def analyze():
    if not GG_DB.exists():
        return {}
    con = sqlite3.connect(GG_DB)
    try:
        rows = con.execute("SELECT collected_at, sport, p1, p2, start, line, over_odds, "
                           "under_odds FROM gg_quotes").fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    # group the quote time-series per match
    series = defaultdict(list)
    for ca, sport, p1, p2, start, line, oo, uo in rows:
        lo, hi = BAND.get(sport, (0, 1e9))
        if line is None or not (lo <= line <= hi):
            continue                                   # drop quarter-market pollution
        nicks = tuple(sorted((str(p1).upper(), str(p2).upper())))
        series[(sport, nicks, str(start)[:16])].append((ca, line, oo, uo))
    flags = _flag_sides()
    per_sport = defaultdict(lambda: {"n": 0, "line_edge": 0.0, "odds_edge": 0.0,
                                     "moved": 0})
    for key, seq in series.items():
        if len(seq) < 2:
            continue
        seq.sort()
        (_, o_line, o_oo, o_uo) = seq[0]
        (_, c_line, c_oo, c_uo) = seq[-1]
        if o_line is None or c_line is None:
            continue
        sport = key[0]
        side = flags.get((sport, key[1], key[2]))
        d = per_sport[sport]
        d["n"] += 1
        if c_line != o_line:
            d["moved"] += 1
        # signed toward our side (default 'under' if we didn't flag it — under is the
        # dominant GG side, but only flagged matches carry real signal)
        s = side or "under"
        line_gain = (c_line - o_line) if s == "under" else (o_line - c_line)
        d["line_edge"] += line_gain
        # our side's opening vs closing decimal price (higher close = we'd have gotten
        # worse odds later -> early was better)
        o_px = o_uo if s == "under" else o_oo
        c_px = c_uo if s == "under" else c_oo
        if o_px and c_px:
            d["odds_edge"] += (o_px - c_px)            # +ve = better price early
    return per_sport


def report():
    per = analyze()
    lines = ["# GG esports — line timing (bet early vs at T-5min)", "",
             f"_{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · from gg_quotes "
             "snapshots · signed toward our flagged side_", "",
             "**Positive line edge = the number drifts toward our side after first post, "
             "so betting EARLY (at ~30-60 min out) beats waiting for the T-5min alert.** "
             "Needs a few days of snapshots per sport to trust.", "",
             "| sport | matches tracked | % line moved | avg line edge (pts) | avg odds edge |",
             "|---|---|---|---|---|"]
    verdict = []
    for sport, d in sorted(per.items()):
        if not d["n"]:
            continue
        le = d["line_edge"] / d["n"]
        oe = d["odds_edge"] / d["n"]
        mv = 100 * d["moved"] / d["n"]
        lines.append(f"| {sport} | {d['n']} | {mv:.0f}% | {le:+.3f} | {oe:+.3f} |")
        if d["n"] >= 40 and le > 0.05:
            verdict.append(f"**{sport}: bet EARLY** — line drifts +{le:.2f} pts toward "
                           "our side on average.")
        elif d["n"] >= 40 and le < -0.05:
            verdict.append(f"**{sport}: wait / bet at alert** — line drifts away "
                           f"({le:+.2f} pts).")
    lines += ["", *(verdict or ["_Not enough snapshots yet for a verdict — the analyzer "
                                "sharpens as gg_quotes fills over the coming days._"]), ""]
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    report()
