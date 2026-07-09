"""WNBA prediction ledger — the learning loop (predict -> grade -> retrain).

Every flagged spot the tool produces is logged here BEFORE the game. The next day the
player's actual box score (ESPN) grades it: did the over hit, and what did they actually
produce vs what we projected. That accumulates the (features -> outcome) pairs the
judgment model trains on — the thing that lets it eventually price the elevated role
itself instead of leaning on the raw hit-rate. No historical closing prop lines exist to
backtest against (books hide them), so this IS the dataset: built forward, on real spots.

The edge it's learning to price, in one line: the beneficiary's ELEVATED-role production
vs a line the book anchored to their SEASON AVERAGE. So we log both, and grade against
what actually happened.

    python wnba_ledger.py --grade     # grade yesterday's logged spots off box scores
    python wnba_ledger.py --report    # win rate, ROI, projection error by stat
    python wnba_ledger.py --train     # fit the projection calibration (once enough data)
"""
from __future__ import annotations

import argparse
import datetime
import json
import statistics as st
from pathlib import Path

import wnba_wowy as W

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_ledger.sqlite"
CAL = HERE / "wnba_proj_cal.json"
STATKEY = {"points": "pts", "rebounds": "reb", "assists": "ast"}
MIN_TRAIN = 30                     # graded spots before a calibration is trustworthy

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions(
  pred_date TEXT, out_player TEXT, player TEXT, team TEXT, opp TEXT,
  stat TEXT, line REAL, odds REAL, book TEXT,
  proj_hit REAL, season_avg REAL, elev_avg REAL, proj_min REAL, n_elev INTEGER,
  ev REAL, stale INTEGER,
  result TEXT, actual REAL, graded INTEGER DEFAULT 0,
  UNIQUE(pred_date, player, stat, line)
);
"""


def _con():
    import sqlite3
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def log_predictions(rows):
    """rows: list of dicts (one per flagged spot). Deduped per (date,player,stat,line)
    so the 4 daily CI runs don't double-log. Returns count newly inserted."""
    if not rows:
        return 0
    con = _con()
    cols = ("pred_date", "out_player", "player", "team", "opp", "stat", "line", "odds",
            "book", "proj_hit", "season_avg", "elev_avg", "proj_min", "n_elev", "ev", "stale")
    n = 0
    for r in rows:
        cur = con.execute(
            f"INSERT OR IGNORE INTO predictions({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", tuple(r.get(c) for c in cols))
        n += cur.rowcount
    con.commit()
    con.close()
    return n


def grade():
    """Grade every ungraded spot whose game has now been played. Matches the player's
    first completed game on/after the prediction date (robust to UTC date rollover in CI),
    reads the actual stat from the ESPN box score, records over/under + the actual value."""
    today = datetime.date.today().isoformat()
    con = _con()
    rows = con.execute(
        "SELECT rowid, pred_date, player, stat, line FROM predictions WHERE graded=0"
    ).fetchall()
    if not rows:
        con.close()
        return 0
    ids = {n: v["id"] for n, v in W.players().items()}
    graded = 0
    log_cache = {}
    for rowid, pred_date, player, stat, line in rows:
        pid = ids.get(player)
        if not pid:
            continue
        if pid not in log_cache:
            try:
                log_cache[pid] = W.game_log(pid)
            except RuntimeError:
                log_cache[pid] = []
        # first completed game on/after the night we predicted for
        cand = sorted((g for g in log_cache[pid]
                       if pred_date <= g["date"][:10] < today), key=lambda g: g["date"])
        if not cand:
            continue                       # not played yet (or DNP with no row) — leave open
        actual = cand[0][STATKEY[stat]]
        res = "over" if actual > line else ("push" if actual == line else "under")
        con.execute("UPDATE predictions SET result=?, actual=?, graded=1 WHERE rowid=?",
                    (res, actual, rowid))
        graded += 1
    con.commit()
    con.close()
    return graded


def report():
    con = _con()
    rows = con.execute(
        "SELECT stat, result, line, elev_avg, season_avg, actual, odds, stale "
        "FROM predictions WHERE graded=1").fetchall()
    n_open = con.execute("SELECT COUNT(*) FROM predictions WHERE graded=0").fetchone()[0]
    con.close()
    print(f"WNBA prediction ledger — {len(rows)} graded, {n_open} awaiting results\n")
    if not rows:
        print("no graded spots yet — accumulating. Grades land the morning after each slate.")
        return
    by = {}
    for stat, res, line, elev, savg, actual, odds, stale in rows:
        by.setdefault(stat, []).append((res, line, elev, actual, odds, stale))
        by.setdefault("ALL", []).append((res, line, elev, actual, odds, stale))
    for stat, rs in sorted(by.items()):
        dec = [r for r in rs if r[0] != "push"]
        wins = sum(1 for r in dec if r[0] == "over")
        units = sum((r[4] - 1) if r[0] == "over" else -1 for r in dec)  # 1u flat, over-bet
        mae = st.mean([abs(r[3] - r[2]) for r in rs])                   # projection error
        wr = wins / len(dec) * 100 if dec else 0
        roi = units / len(dec) * 100 if dec else 0
        print(f"  {stat:9} {wins}-{len(dec)-wins}  win {wr:4.0f}%  ROI {roi:+5.1f}%  "
              f"proj MAE {mae:.1f}")
    # is the 'stale line' read actually the edge? compare stale vs not
    stale_dec = [r for r in by["ALL"] if r[0] != "push" and r[5]]
    if stale_dec:
        sw = sum(1 for r in stale_dec if r[0] == "over") / len(stale_dec) * 100
        print(f"\n  stale-line spots (line anchored near season avg): "
              f"{sw:.0f}% over on {len(stale_dec)} — this is the mechanism, watch it.")


def train():
    """Fit the projection calibration: actual ~ a + b·elev_avg via least squares, once
    enough graded spots exist. Corrects the raw elevated-average's bias (it tends to run
    hot — best games get remembered). Written to wnba_proj_cal.json for the projector to
    apply. Honest until then: reports how far off the sample is."""
    con = _con()
    rows = con.execute(
        "SELECT elev_avg, actual FROM predictions WHERE graded=1 AND result!='push'"
    ).fetchall()
    con.close()
    n = len(rows)
    if n < MIN_TRAIN:
        print(f"train: {n}/{MIN_TRAIN} graded spots — need more before a calibration is "
              f"trustworthy. Loop is accumulating; re-run as results land.")
        return
    xs = [e for e, _ in rows]
    ys = [a for _, a in rows]
    mx, my = st.mean(xs), st.mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in rows) / vx if vx else 1.0
    a = my - b * mx
    mae_raw = st.mean([abs(y - x) for x, y in rows])
    mae_cal = st.mean([abs(y - (a + b * x)) for x, y in rows])
    CAL.write_text(json.dumps({"a": round(a, 3), "b": round(b, 3), "n": n,
                               "mae_raw": round(mae_raw, 2),
                               "mae_cal": round(mae_cal, 2)}, indent=1))
    print(f"train: calibrated actual = {a:+.2f} + {b:.2f}·elev_avg  (n={n})")
    print(f"       projection error {mae_raw:.2f} -> {mae_cal:.2f} MAE "
          f"({'improved' if mae_cal < mae_raw else 'no gain — raw avg already fine'})")


def calibrate(elev_avg):
    """Apply the fitted calibration to a raw elevated-average projection (identity until
    trained)."""
    if not CAL.exists():
        return elev_avg
    c = json.loads(CAL.read_text())
    return c["a"] + c["b"] * elev_avg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--train", action="store_true")
    args = ap.parse_args()
    if args.grade:
        print(f"graded {grade()} spots")
    if args.train:
        train()
    if args.report or not (args.grade or args.train):
        report()


if __name__ == "__main__":
    main()
