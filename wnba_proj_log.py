"""Background PROJECTION TRACKER — a learning loop, not a display.

Every scan, the model's full per-player projection (minutes + points/rebounds/assists + the
context assumptions it made: the out player, the role bump, lineup status, position, basis) is
logged BEFORE the game. After games finish it's graded against the actual box score, and
`--analyze` finds WHERE the model systematically misses — is it over-projecting MINUTES, or the
production RATE, and for which segments — so the projection can be refined toward accuracy.

    python wnba_proj_log.py --grade      # grade finished games off box scores
    python wnba_proj_log.py --analyze     # systematic-error report -> wnba_proj_accuracy.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import statistics as st
from pathlib import Path

import wnba_wowy as W

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_proj_log.sqlite"
REPORT = HERE / "wnba_proj_accuracy.md"
MIN_GRADED = 25
STATS = ("pts", "reb", "ast")

SCHEMA = """CREATE TABLE IF NOT EXISTS projections(
  date TEXT, pid TEXT, player TEXT, team TEXT, opp TEXT, out_player TEXT, confidence TEXT,
  basis TEXT, n_games INTEGER, pos TEXT, d_min REAL,
  proj_min REAL, proj_pts REAL, proj_reb REAL, proj_ast REAL, logged_at TEXT,
  actual_min REAL, actual_pts REAL, actual_reb REAL, actual_ast REAL, graded INTEGER DEFAULT 0,
  UNIQUE(date, pid));"""
COLS = ("date", "pid", "player", "team", "opp", "out_player", "confidence", "basis",
        "n_games", "pos", "d_min", "proj_min", "proj_pts", "proj_reb", "proj_ast", "logged_at")


def _con():
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    return con


def log(rows):
    """Upsert per (date, pid) — keep the latest projection (closest to tip = best lineup read)."""
    if not rows:
        return 0
    con = _con()
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    sets = ",".join(f"{c}=excluded.{c}" for c in COLS if c not in ("date", "pid"))
    n = 0
    for r in rows:
        r = {**r, "logged_at": ts}
        n += con.execute(
            f"INSERT INTO projections({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))}) "
            f"ON CONFLICT(date, pid) DO UPDATE SET {sets}",
            tuple(r.get(c) for c in COLS)).rowcount
    con.commit()
    con.close()
    return n


def grade():
    con = _con()
    rows = con.execute("SELECT rowid, date, pid, opp FROM projections WHERE graded=0").fetchall()
    if not rows:
        con.close()
        return 0
    ids = {v["id"]: n for n, v in W.players().items()}     # ensure name cache warm (pid is the key)
    cache, graded = {}, 0
    for rid, date, pid, opp in rows:
        if pid not in cache:
            try:
                cache[pid] = W.game_log(pid)
            except RuntimeError:
                cache[pid] = []
        cand = sorted((g for g in cache[pid]
                       if g.get("result") and g["date"][:10] >= date
                       and (not opp or (g.get("matchup") or "").upper() == opp.upper())),
                      key=lambda g: g["date"])
        if not cand:
            continue
        g = cand[0]
        con.execute("UPDATE projections SET actual_min=?, actual_pts=?, actual_reb=?, "
                    "actual_ast=?, graded=1 WHERE rowid=?",
                    (g["min"], g["pts"], g["reb"], g["ast"], rid))
        graded += 1
    con.commit()
    con.close()
    return graded


def _bias(vals):
    return (st.mean(vals), st.mean(abs(v) for v in vals)) if vals else (0.0, 0.0)


def analyze():
    con = _con()
    con.row_factory = sqlite3.Row
    R = [dict(r) for r in con.execute("SELECT * FROM projections WHERE graded=1 AND actual_min>0")]
    con.close()
    L = [f"# WNBA projection accuracy — learning loop", "",
         f"_{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · {len(R)} graded projections_", ""]
    if len(R) < MIN_GRADED:
        L.append(f"Accumulating — {len(R)}/{MIN_GRADED} graded before the bias read is trustworthy.")
        REPORT.write_text("\n".join(L) + "\n")
        print(f"proj analyze: {len(R)}/{MIN_GRADED} graded — accumulating")
        return

    # 1) MINUTES — the driver. bias = actual - proj (positive = we UNDER-projected minutes)
    mins = [(r["actual_min"] - r["proj_min"]) for r in R]
    mb, mmae = _bias(mins)
    L += ["## Minutes", "```",
          f"overall: proj {'HIGH' if mb < 0 else 'LOW'} by {abs(mb):.1f} min (MAE {mmae:.1f}, n{len(R)})"]
    for tier in ("confirmed", "likely", "projected", "bench"):
        g = [r["actual_min"] - r["proj_min"] for r in R if r["confidence"] == tier]
        if len(g) >= 8:
            b, _ = _bias(g)
            L.append(f"  {tier:10} proj {'HIGH' if b < 0 else 'LOW'} by {abs(b):.1f} min  (n{len(g)})")
    L += ["```", ""]

    # 2) PER-STAT — bias + decompose error into a MINUTES miss vs a RATE miss
    L += ["## Production — is the miss MINUTES or RATE?", "```",
          f"{'stat':5}{'proj-bias':>11}{'MAE':>7}{'from minutes':>14}{'from rate':>11}"]
    for s in STATS:
        pk = f"proj_{s}"
        ak = f"actual_{s}"
        errs, mcomp, rcomp = [], [], []
        for r in R:
            if not r[pk] or not r["proj_min"] or not r["actual_min"]:
                continue
            errs.append(r[ak] - r[pk])
            rate_p = r[pk] / r["proj_min"]
            rate_a = r[ak] / r["actual_min"]
            mcomp.append((r["actual_min"] - r["proj_min"]) * rate_p)   # error from minutes miss
            rcomp.append(r["actual_min"] * (rate_a - rate_p))          # error from rate miss
        b, mae = _bias(errs)
        L.append(f"{s:5}{b:>+11.2f}{mae:>7.2f}{st.mean(mcomp):>+14.2f}{st.mean(rcomp):>+11.2f}")
    L += ["```",
          "(proj-bias +/- = actual over/under our projection; 'from minutes' vs 'from rate' says "
          "whether the miss is playing-time or per-minute production)", ""]

    # 3) the single biggest SYSTEMATIC miss across segments (effect x sqrt(n))
    segs = []
    for s in STATS:
        pk, ak = f"proj_{s}", f"actual_{s}"
        for field, label in (("basis", "basis"), ("pos", "pos"), ("confidence", "lineup")):
            groups = {}
            for r in R:
                if r[pk] and r.get(field) is not None:
                    groups.setdefault(r[field], []).append(r[ak] - r[pk])
            for gv, errs in groups.items():
                if len(errs) >= 10:
                    b, _ = _bias(errs)
                    segs.append((abs(b) * len(errs) ** 0.5, s, f"{label}={gv}", b, len(errs)))
    segs.sort(reverse=True)
    if segs:
        L += ["## Biggest systematic misses (rank = |bias| x sqrt(n))", "```"]
        for _, s, seg, b, n in segs[:6]:
            L.append(f"  {s} {seg:20} {'over' if b > 0 else 'under'}-produces our proj by "
                     f"{abs(b):.2f}  (n{n})")
        L += ["```",
              "-> refine: shrink the projection where we consistently run high; lift where we run "
              "low. Minutes bias feeds straight through, so fix minutes first.", ""]

    REPORT.write_text("\n".join(L) + "\n")
    print(f"proj analyze: {len(R)} graded · minutes bias {mb:+.1f} · wrote {REPORT.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    args = ap.parse_args()
    if args.grade:
        print(f"proj grade: {grade()} newly graded")
    if args.analyze or not args.grade:
        analyze()


if __name__ == "__main__":
    main()
