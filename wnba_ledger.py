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
PLAYED = HERE / "wnba_played.txt"      # durable user-played marks (plain text: merges clean,
                                       # re-applied on every DB open so a CI DB-reset can't lose them)
STATKEY = {"points": "pts", "rebounds": "reb", "assists": "ast"}
MIN_TRAIN = 30                     # graded spots before a calibration is trustworthy

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions(
  pred_date TEXT, out_player TEXT, player TEXT, team TEXT, opp TEXT,
  stat TEXT, line REAL, odds REAL, book TEXT,
  proj_hit REAL, season_avg REAL, elev_avg REAL, proj_min REAL, n_elev INTEGER,
  ev REAL, stale INTEGER,
  d_stat REAL, d_fga REAL, d_min REAL, driver REAL, vac REAL,
  total REAL, pace REAL, opp_def REAL, d_fta REAL, d_3pa REAL,
  basis TEXT, samples TEXT, confidence TEXT,
  result TEXT, actual REAL, graded INTEGER DEFAULT 0,
  UNIQUE(pred_date, player, stat, line)
);
"""
# Model features, migrated into older DBs in place. driver = per-stat deciding delta
# (FGA rise for points, reb/ast rise otherwise); vac = out player's own avg in the stat
# (pool size); total/pace/opp_def = matchup environment; d_fta/d_3pa = points channels.
_MIGRATE = ("d_stat", "d_fga", "d_min", "driver", "vac",
            "total", "pace", "opp_def", "d_fta", "d_3pa")
_MIGRATE_TEXT = ("basis", "samples", "confidence")   # confidence = starter status label


def _con():
    import sqlite3
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
    for col in _MIGRATE:
        if col not in have:
            con.execute(f"ALTER TABLE predictions ADD COLUMN {col} REAL")
    for col in _MIGRATE_TEXT:
        if col not in have:
            con.execute(f"ALTER TABLE predictions ADD COLUMN {col} TEXT")
    if "played" not in have:      # user-approval label: did the user actually bet this flag?
        con.execute("ALTER TABLE predictions ADD COLUMN played INTEGER DEFAULT 0")
    con.commit()
    _apply_played(con)            # re-derive played marks from the durable text file
    return con


def _apply_played(con):
    """Re-apply the durable played marks (wnba_played.txt) onto the DB, so a fresh/reset CI
    database always reflects what the user bet."""
    if not PLAYED.exists():
        return
    for ln in PLAYED.read_text().splitlines():
        p = ln.strip().split("|")
        if len(p) == 4:
            con.execute("UPDATE predictions SET played=1 WHERE pred_date=? AND player LIKE ? "
                        "AND stat=? AND line=?", (p[0], f"%{p[1]}%", p[2], float(p[3])))
    con.commit()


def mark_played(specs, date=None):
    """Tag flagged rows the USER actually bet — the human-approval label the learner trains
    on (what distinguishes the plays he takes from the ones he passes). Writes to the durable
    text file AND the DB. specs = list of (player_substr, stat, line); date defaults to the
    latest slate. Returns count matched in the DB."""
    con = _con()
    if date is None:
        row = con.execute("SELECT MAX(pred_date) FROM predictions").fetchone()
        date = row[0] if row else None
    keys = set(PLAYED.read_text().splitlines()) if PLAYED.exists() else set()
    n = 0
    for player, stat, line in specs:
        keys.add(f"{date}|{player}|{stat}|{line:g}")
        n += con.execute(
            "UPDATE predictions SET played=1 WHERE pred_date=? AND player LIKE ? "
            "AND stat=? AND line=?", (date, f"%{player}%", stat, float(line))).rowcount
    PLAYED.write_text("\n".join(sorted(keys)))
    con.commit()
    con.close()
    return n


def log_predictions(rows):
    """rows: list of dicts (one per flagged spot). Deduped per (date,player,stat,line)
    so the 4 daily CI runs don't double-log. Returns count newly inserted."""
    if not rows:
        return 0
    con = _con()
    cols = ("pred_date", "out_player", "player", "team", "opp", "stat", "line", "odds",
            "book", "proj_hit", "season_avg", "elev_avg", "proj_min", "n_elev", "ev", "stale",
            "d_stat", "d_fga", "d_min", "driver", "vac",
            "total", "pace", "opp_def", "d_fta", "d_3pa", "basis", "samples", "confidence")
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


# The winner/loser-similarity learner: what do WINNING bets have in common vs losing ones.
LEARN = HERE / "wnba_learn.json"
MIN_LEARN = 40                       # graded bets before a profile means anything
LEARN_FEATURES = ["proj_hit", "ev", "n_elev", "d_min", "d_fga", "d_stat", "driver", "vac",
                  "elev_avg", "season_avg", "proj_min", "total", "pace", "opp_def"]


def learn():
    """Find what separates WINNERS from LOSERS across every graded bet — the judgment
    learning the user asked for, made interpretable. For each feature, the winner-mean vs
    loser-mean and a standardized separation (Cohen's d); the strongest separators are the
    traits your winning spots share. Writes wnba_learn.json (per-feature separations) so the
    flagger can eventually re-weight EV toward the winning profile. Gated on sample size —
    below a real N this is noise, and it says so."""
    con = _con()
    rows = con.execute(
        f"SELECT result, {','.join(LEARN_FEATURES)} FROM predictions "
        f"WHERE graded=1 AND result IN ('over','under')").fetchall()
    con.close()
    wins = [r for r in rows if r[0] == "over"]
    loss = [r for r in rows if r[0] == "under"]
    n = len(rows)
    if n < MIN_LEARN or len(wins) < 8 or len(loss) < 8:
        print(f"learn: {n}/{MIN_LEARN} graded bets ({len(wins)}W-{len(loss)}L) — accumulating. "
              f"A winner/loser profile on a thinner sample is just noise; the loop is "
              f"building the dataset (fast once NBA starts in Oct).")
        return
    seps = []
    for i, f in enumerate(LEARN_FEATURES, start=1):
        wv = [r[i] for r in wins if r[i] is not None]
        lv = [r[i] for r in loss if r[i] is not None]
        if len(wv) < 5 or len(lv) < 5:
            continue
        mw, ml = st.mean(wv), st.mean(lv)
        sd = st.pstdev(wv + lv) or 1.0
        seps.append((round((mw - ml) / sd, 3), f, round(mw, 2), round(ml, 2)))
    seps.sort(key=lambda x: -abs(x[0]))
    print(f"learn: winner vs loser profile over {n} graded bets ({len(wins)}W-{len(loss)}L)\n")
    for d, f, mw, ml in seps:
        tag = "WINNERS higher" if d > 0 else "losers higher"
        print(f"  {f:11} win {mw:>6} vs loss {ml:>6}  sep {d:+.2f}  {tag}")
    LEARN.write_text(json.dumps({"n": n, "w": len(wins), "l": len(loss),
                                 "separations": {f: d for d, f, _, _ in seps}}, indent=1))
    print("\n  -> wrote wnba_learn.json. The top separators ARE the shared traits of your "
          "winners; as the sample grows they firm up and can re-weight the flagger's EV.")


SELECT = HERE / "wnba_select.json"
# selection also cares about price + line depth, so add them to the model features
SELECT_FEATURES = LEARN_FEATURES + ["odds", "line"]


def learn_selection():
    """What separates the flags the USER PLAYS from the ones he PASSES — his selection filter,
    learned from the `played` label rather than win/loss (juice aversion, role-jump preference,
    …). Same Cohen's-d method as learn(). Independent of grading, so it fires as soon as there
    are enough played+passed flags. This is the human-in-the-loop signal: eventually the
    flagger can pre-rank toward what he'd actually bet."""
    con = _con()
    rows = con.execute(
        f"SELECT played, {','.join(SELECT_FEATURES)} FROM predictions").fetchall()
    con.close()
    played = [r for r in rows if r[0] == 1]
    passed = [r for r in rows if not r[0]]
    if len(played) < 8 or len(passed) < 15:
        print(f"\nselect: {len(played)} played / {len(passed)} passed — accumulating your "
              f"selection profile (needs ~8 played / 15 passed to separate signal from noise).")
        return
    seps = []
    for i, f in enumerate(SELECT_FEATURES, start=1):
        pv = [r[i] for r in played if r[i] is not None]
        qv = [r[i] for r in passed if r[i] is not None]
        if len(pv) < 5 or len(qv) < 5:
            continue
        mp, mq = st.mean(pv), st.mean(qv)
        sd = st.pstdev(pv + qv) or 1.0
        seps.append((round((mp - mq) / sd, 3), f, round(mp, 2), round(mq, 2)))
    seps.sort(key=lambda x: -abs(x[0]))
    print(f"\nselect: PLAYED vs PASSED over {len(played)}+{len(passed)} flags\n")
    for d, f, mp, mq in seps:
        print(f"  {f:11} play {mp:>7} vs pass {mq:>7}  sep {d:+.2f}  "
              f"{'PLAYS higher' if d > 0 else 'passes higher'}")
    SELECT.write_text(json.dumps({"played": len(played), "passed": len(passed),
                                  "separations": {f: d for d, f, _, _ in seps}}, indent=1))
    print("  -> wrote wnba_select.json = your selection filter (what makes you take a flag).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--played", nargs="+", metavar="PLAYER/STAT/LINE",
                    help="mark flagged rows the user bet, e.g. 'Billings/rebounds/5.5'")
    args = ap.parse_args()
    if args.played:
        specs = [(s.rsplit("/", 2)[0], s.rsplit("/", 2)[1], float(s.rsplit("/", 2)[2]))
                 for s in args.played]
        print(f"marked {mark_played(specs)} row(s) as played")
        return
    if args.grade:
        print(f"graded {grade()} spots")
    if args.train:
        train()
    if args.learn:
        learn()
        learn_selection()
    if args.report or not (args.grade or args.train or args.learn):
        report()


if __name__ == "__main__":
    main()
