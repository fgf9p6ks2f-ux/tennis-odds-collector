"""Calibrate SIT_PROB from reality — log every questionable designation, resolve whether the
player actually SAT, and recompute the empirical sit-rate per designation.

The watchlist projects "if he sits" beneficiaries tagged with a sit-probability. That prior
(Questionable/GTD 0.50, Doubtful 0.80 in wnba_tonight.SIT_PROB) is a guess until enough real
resolutions are observed. This closes the loop:

  record()      — each poll, log every KEY questionable/doubtful/GTD player on tonight's slate.
                  INSERT-OR-IGNORE keyed on (slate_date, player) = the FIRST time we saw them
                  uncertain that day (a Q that later escalates to OUT and sits still counts as a
                  questionable that sat — exactly the P(sit | first seen Questionable) we want).
  resolve()     — for each logged row, read the player's game log: a line ON that date = PLAYED;
                  none (with the log proven to extend PAST that date, so it's not just lag) = SAT.
                  Players are only logged when their team is on that day's slate, so a missing line
                  is a genuine scratch, not a bye.
  recalibrate() — write wnba_sit_prob.json = empirical P(sit | designation) for any designation
                  with >= MIN_N resolved rows; wnba_tonight.sit_prob() prefers it over the prior.

    python wnba_question_log.py --resolve --recalibrate   # settle past dates + rewrite the override
    python wnba_question_log.py --report                  # current counts + empirical rates
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import wnba_wowy as W

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_question_log.sqlite"
OVERRIDE = HERE / "wnba_sit_prob.json"
MIN_N = 20                       # resolved observations before a designation's rate overrides the prior

DDL = """CREATE TABLE IF NOT EXISTS question_log (
  date TEXT, player TEXT, team TEXT, designation TEXT, mpg REAL,
  first_seen TEXT, tip TEXT, resolved INTEGER DEFAULT 0, sat INTEGER,
  PRIMARY KEY(date, player))"""


def _con():
    con = sqlite3.connect(DB)
    con.execute(DDL)
    if "tip" not in {r[1] for r in con.execute("PRAGMA table_info(question_log)")}:
        con.execute("ALTER TABLE question_log ADD COLUMN tip TEXT")   # migrate older DBs in place
    return con


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def record(date, observations):
    """observations = iterable of (player, team, designation, mpg, tip). `tip` = the game's tip time
    (naive-UTC iso) so lead-time = tip - first_seen is recoverable. Log each once per (date, player)
    — INSERT-OR-IGNORE keeps the FIRST (earliest) sighting, so first_seen is the true lead anchor."""
    con = _con()
    ts = _now()
    n = 0
    for row in observations:
        player, team, desig, mpg = row[0], row[1], row[2], row[3]
        tip = row[4] if len(row) > 4 else None
        cur = con.execute(
            "INSERT OR IGNORE INTO question_log(date, player, team, designation, mpg, first_seen, tip) "
            "VALUES (?,?,?,?,?,?,?)",
            (date, player, team, (desig or "").strip().upper(), mpg, ts, tip))
        n += cur.rowcount
    con.commit()
    con.close()
    return n


def first_seen_map(date):
    """{player: first_seen_iso} for a slate date — the earliest moment we logged each player
    questionable, so callers can compute lead time = tip - first_seen at projection time."""
    con = _con()
    r = con.execute("SELECT player, first_seen FROM question_log WHERE date=?", (date,)).fetchall()
    con.close()
    return {p: fs for p, fs in r}


def resolve(game_log=None, players=None):
    """Settle unresolved rows. PLAYED (sat=0) = a game-log line on that date. SAT (sat=1) = no line,
    AND the log has a game STRICTLY AFTER that date (proving it's current past the date, so the gap
    is a real scratch, not log lag). Rows we can't yet confirm stay pending for a later pass."""
    game_log = game_log or W.game_log
    pl = players if players is not None else W.players()
    idmap = {n: v["id"] for n, v in pl.items()}
    con = _con()
    rows = con.execute("SELECT date, player FROM question_log WHERE resolved=0").fetchall()
    done = 0
    for date, player in rows:
        pid = idmap.get(player)
        if not pid:
            continue                                  # unmappable name -> retry a later pass
        try:
            lg = game_log(pid)
        except Exception:
            continue
        days = [g["date"][:10] for g in lg]
        if date in days:
            sat = 0
        elif any(d > date for d in days):             # log extends past the date -> confirmed scratch
            sat = 1
        else:
            continue                                  # no game after it yet -> can't confirm, retry
        con.execute("UPDATE question_log SET resolved=1, sat=? WHERE date=? AND player=?",
                    (sat, date, player))
        done += 1
    con.commit()
    con.close()
    return done


def _lead_between(first_seen, tip):
    """Hours from first_seen to tip (both naive-UTC iso). None if unknown/unparseable."""
    if not first_seen or not tip:
        return None
    try:
        return (dt.datetime.fromisoformat(tip)
                - dt.datetime.fromisoformat(first_seen)).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def rates(min_n=0):
    """{(designation, bucket): (n, P(sit))} over resolved rows. bucket = early/late/unk from the
    lead time (tip - first_seen), split via wnba_tonight.lead_bucket (one source of truth). Every
    row ALSO contributes to (designation, 'unk') = the pooled designation rate, so a designation can
    beat the prior on pooled evidence before any single lead bucket reaches n."""
    from wnba_tonight import lead_bucket
    con = _con()
    r = con.execute("SELECT designation, first_seen, tip, sat FROM question_log "
                    "WHERE resolved=1").fetchall()
    con.close()
    agg = defaultdict(lambda: [0, 0])                    # key -> [n, sat_sum]
    for desig, fs, tip, sat in r:
        b = lead_bucket(_lead_between(fs, tip))
        for key in ((desig, b), (desig, "unk")):
            agg[key][0] += 1
            agg[key][1] += (sat or 0)
    return {k: (n, s / n) for k, (n, s) in agg.items() if n and n >= min_n}


def recalibrate(min_n=MIN_N):
    """Write wnba_sit_prob.json = empirical P(sit) keyed 'DESIGNATION|bucket' for any bucket with
    enough resolved n. sit_prob() prefers the specific bucket, then the pooled 'DESIG|unk', then the
    prior — so calibration sharpens from prior -> pooled -> lead-conditional as data accrues."""
    emp = {f"{desig}|{bucket}": round(p, 3) for (desig, bucket), (n, p) in rates(min_n).items()}
    OVERRIDE.write_text(json.dumps(emp, indent=1))
    return emp


def _report():
    con = _con()
    tot = con.execute("SELECT COUNT(*), COALESCE(SUM(resolved), 0) FROM question_log").fetchone()
    con.close()
    print(f"question_log: {tot[0]} logged, {tot[1]} resolved")
    allr = rates()
    if not allr:
        print("  no resolved observations yet — sit_prob() runs on the lead-aware prior")
        return
    for (d, b), (n, p) in sorted(allr.items()):
        flag = "  <- OVERRIDES prior" if n >= MIN_N else f"  (need {MIN_N - n} more)"
        print(f"  {d:14} {b:5} n={n:3}  P(sit)={p:.2f}{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve", action="store_true", help="settle past questionable observations")
    ap.add_argument("--recalibrate", action="store_true", help="rewrite wnba_sit_prob.json from data")
    ap.add_argument("--report", action="store_true", help="show counts + empirical rates")
    a = ap.parse_args()
    if a.resolve:
        print(f"resolved {resolve()} questionable observations")
    if a.recalibrate:
        emp = recalibrate()
        print(f"recalibrated sit_prob overrides: {emp or '(none yet — insufficient n, using prior)'}")
    if a.report or not (a.resolve or a.recalibrate):
        _report()


if __name__ == "__main__":
    main()
