"""NBA port · WOWY engine v2 — beneficiary priors the live loop will consume.

Phase 0 validated the premise (absence game 1-2 × proj-line margin ≥5 → 66.5% over on
PRA). v2 hardens it into a production engine, each feature A/B'd on the same walk-forward
before it ships (golden rule):

  TENURE   only count "X out" games on/after X's first appearance with the team — kills
           the pre-arrival pollution where a summer signing's whole prior season reads
           as "out" games.
  SHRINK   small WOWY samples are noisy: d_min shrunk by n/(n+3); X-out per-min rates
           shrunk toward B's own baseline rate with 60 pseudo-minutes.
  POOL     prior window = trailing 400 DAYS (spans the season boundary) instead of
           same-season — the October cold-start fix: opening-month games have last
           season's WOWY priors instead of nothing.
  PER-STAT pts / reb / ast / fg3m / pra projections (the live system bets specific props).

stats.nba.com is Mac-only: this builds OFFLINE here; `--build` emits nba_wowy.sqlite
(pair priors + player baselines), committed to the repo for the loop. In-season the
current-day layer comes from cdn.nba.com boxscores accruing in the loop itself.

    python nba_wowy.py --validate     # A/B the v2 features vs the Phase-0 baseline
    python nba_wowy.py --build        # emit nba_wowy.sqlite from the trailing window
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
LOGS = HERE / "nba_gamelogs.sqlite"
OUT_DB = HERE / "nba_wowy.sqlite"

STAR_MIN = 28.0
ACTIVE_DAYS = 12
MIN_PRIOR_APPS = 5
MIN_WOWY = 2
POOL_DAYS = 400
K_DMIN = 3.0            # d_min shrink pseudo-games
K_MIN = 60.0            # rate shrink pseudo-minutes
STATS = ("pts", "reb", "ast", "fg3m", "pra")
# per-stat flag margins (proj must beat the proxy line by this much) — scale with the stat
MARGIN = {"pra": 5.0, "pts": 3.0, "reb": 2.0, "ast": 2.0, "fg3m": 0.8}


def _d(s):
    return dt.date.fromisoformat(s)


def load_team_games(pooled):
    """[(team)] -> ordered [(date, gid, {pid: (min, {stat: val})})]. pooled=True merges
    seasons per franchise (the 400-day window is applied at walk time)."""
    con = sqlite3.connect(LOGS)
    rows = con.execute("SELECT season, game_id, date, team, player_id, player, min, "
                       "pts, reb, ast, fg3m FROM logs ORDER BY date, game_id").fetchall()
    con.close()
    names = {}
    tg = defaultdict(list)
    cur = {}
    for season, gid, date, team, pid, player, mn, p, r, a, t3 in rows:
        key = (team, gid) if pooled else (season, team, gid)
        if key not in cur:
            cur[key] = {}
            tg[team if pooled else (season, team)].append((date, gid, cur[key]))
        cur[key][pid] = (mn, {"pts": p, "reb": r, "ast": a, "fg3m": t3, "pra": p + r + a})
        names[pid] = player
    for k in tg:
        tg[k].sort(key=lambda g: g[0])
    return tg, names


def walk(cfg):
    """Walk-forward flags under a config; returns per-stat {cell: [overs, n]}.
    cfg: tenure, shrink, pooled (bools)."""
    tg, _ = load_team_games(cfg["pooled"])
    res = defaultdict(lambda: [0, 0])
    octnov = defaultdict(lambda: [0, 0])
    for key, games in tg.items():
        first_app = {}                      # pid -> first date seen with this team key
        for i, (date, gid, box) in enumerate(games):
            for pid in box:
                first_app.setdefault(pid, date)
            if i < 12:
                continue
            lo = 0
            if cfg["pooled"]:
                cut = (_d(date) - dt.timedelta(days=POOL_DAYS)).isoformat()
                while lo < i and games[lo][0] < cut:
                    lo += 1
            prior = games[lo:i]
            if len(prior) < 12:
                continue
            played_by = defaultdict(list)
            for pd, pg, pbox in prior:
                for pid, (mn, st) in pbox.items():
                    played_by[pid].append((pd, mn, st))
            absents = [pid for pid, apps in played_by.items()
                       if pid not in box and len(apps) >= 10
                       and sum(a[1] for a in apps[-10:]) / 10 >= STAR_MIN
                       and (_d(date) - _d(apps[-1][0])).days <= ACTIVE_DAYS]
            if not absents:
                continue
            X = max(absents, key=lambda p: sum(a[1] for a in played_by[p][-10:]))
            aidx = 1
            for pd, pg, pbox in reversed(prior):
                if X in pbox:
                    break
                aidx += 1
            if aidx > 2:                    # production cell: news window only
                continue
            x_first = first_app.get(X, "0000")
            out_g, in_g = [], []
            for pd, pg, pbox in prior:
                if cfg["tenure"] and pd < x_first:
                    continue
                (out_g if X not in pbox else in_g).append(pbox)
            for B, (bmn, bst) in box.items():
                if B == X:
                    continue
                apps = played_by.get(B, [])
                if len(apps) < MIN_PRIOR_APPS:
                    continue
                b_out = [pbox[B] for pbox in out_g if B in pbox]
                b_in = [pbox[B] for pbox in in_g if B in pbox]
                if len(b_out) < MIN_WOWY or len(b_in) < 3:
                    continue
                base_min = sum(a[1] for a in apps[-10:]) / min(10, len(apps))
                min_out = sum(m for m, _ in b_out)
                d_min = min_out / len(b_out) - sum(m for m, _ in b_in) / len(b_in)
                if cfg["shrink"]:
                    d_min *= len(b_out) / (len(b_out) + K_DMIN)
                for stat in STATS:
                    tot_out = sum(st[stat] for _, st in b_out)
                    base_rate = (sum(a[2][stat] for a in apps[-10:])
                                 / max(1e-9, sum(a[1] for a in apps[-10:])))
                    if cfg["shrink"]:
                        rate = (tot_out + base_rate * K_MIN) / (min_out + K_MIN)
                    else:
                        rate = tot_out / max(1e-9, min_out)
                    proj = rate * (base_min + d_min)
                    line = median(a[2][stat] for a in apps[-10:])
                    if proj - line < MARGIN[stat]:
                        continue
                    over = 1 if bst[stat] > line else 0
                    res[stat][0] += over
                    res[stat][1] += 1
                    if date[5:7] in ("10", "11"):
                        octnov[stat][0] += over
                        octnov[stat][1] += 1
    return res, octnov


def validate():
    print("=== WOWY v2 A/B — production cell (absence game 1-2, per-stat margins) ===\n")
    cfgs = [("v1 baseline (Phase 0)", {"tenure": 0, "shrink": 0, "pooled": 0}),
            ("+ tenure fix",          {"tenure": 1, "shrink": 0, "pooled": 0}),
            ("+ shrinkage",           {"tenure": 1, "shrink": 1, "pooled": 0}),
            ("+ cross-season pool",   {"tenure": 1, "shrink": 1, "pooled": 1})]
    for label, cfg in cfgs:
        res, octnov = walk(cfg)
        o, n = res["pra"]
        oo, on = octnov["pra"]
        print(f"{label:<24} PRA: {o}/{n} = {o/max(1,n)*100:5.1f}%"
              f"   ·   Oct-Nov: {oo}/{on} = {oo/max(1,on)*100:5.1f}%")
    print("\nper-stat grid for the FULL v2 config:")
    res, octnov = walk({"tenure": 1, "shrink": 1, "pooled": 1})
    for stat in STATS:
        o, n = res[stat]
        oo, on = octnov[stat]
        print(f"  {stat:<5} margin>={MARGIN[stat]:<4} {o:>5}/{n:<6} = {o/max(1,n)*100:5.1f}%"
              f"   Oct-Nov {oo}/{on} = {oo/max(1,on)*100:.1f}%")


def build():
    """Emit nba_wowy.sqlite: pair priors + player baselines from the trailing 400 days
    (through the end of the banked data). The live loop joins these to the injury feed."""
    tg, names = load_team_games(pooled=True)
    con = sqlite3.connect(OUT_DB)
    con.executescript("""
    DROP TABLE IF EXISTS pairs; DROP TABLE IF EXISTS baselines;
    CREATE TABLE pairs (team TEXT, x_id INT, x_name TEXT, b_id INT, b_name TEXT,
        n_out INT, n_in INT, d_min REAL,
        rate_pts REAL, rate_reb REAL, rate_ast REAL, rate_fg3m REAL, rate_pra REAL,
        PRIMARY KEY (team, x_id, b_id));
    CREATE TABLE baselines (b_id INT PRIMARY KEY, b_name TEXT, team TEXT, last_date TEXT,
        base_min REAL, med_pts REAL, med_reb REAL, med_ast REAL, med_fg3m REAL, med_pra REAL)
    """)
    prows, brows = [], {}
    for team, games in tg.items():
        if not games:
            continue
        end = games[-1][0]
        cut = (_d(end) - dt.timedelta(days=POOL_DAYS)).isoformat()
        window = [g for g in games if g[0] >= cut]
        played_by = defaultdict(list)
        first_app = {}
        for pd, pg, pbox in window:
            for pid, (mn, st) in pbox.items():
                played_by[pid].append((pd, mn, st))
                first_app.setdefault(pid, pd)
        # X candidates: 25+ min avg over last 15 apps in window (production threshold —
        # flag-time can demand more; the table just needs coverage)
        xs = [pid for pid, apps in played_by.items()
              if len(apps) >= 10 and sum(a[1] for a in apps[-15:]) / min(15, len(apps)) >= 25]
        for X in xs:
            xf = first_app[X]
            out_g, in_g = [], []
            for pd, pg, pbox in window:
                if pd < xf:
                    continue
                (out_g if X not in pbox else in_g).append(pbox)
            for B, apps in played_by.items():
                if B == X or len(apps) < MIN_PRIOR_APPS:
                    continue
                b_out = [pbox[B] for pbox in out_g if B in pbox]
                b_in = [pbox[B] for pbox in in_g if B in pbox]
                if len(b_out) < MIN_WOWY or len(b_in) < 3:
                    continue
                min_out = sum(m for m, _ in b_out)
                d_min = (min_out / len(b_out) - sum(m for m, _ in b_in) / len(b_in))
                d_min *= len(b_out) / (len(b_out) + K_DMIN)
                base_rate = {s: (sum(a[2][s] for a in apps[-10:])
                                 / max(1e-9, sum(a[1] for a in apps[-10:]))) for s in STATS}
                rates = {s: (sum(st[s] for _, st in b_out) + base_rate[s] * K_MIN)
                            / (min_out + K_MIN) for s in STATS}
                prows.append((team, X, names[X], B, names[B], len(b_out), len(b_in),
                              round(d_min, 2), *(round(rates[s], 4) for s in STATS)))
        for B, apps in played_by.items():
            if len(apps) < MIN_PRIOR_APPS:
                continue
            last = apps[-10:]
            brows[B] = (B, names[B], team, apps[-1][0],
                        round(sum(a[1] for a in last) / len(last), 1),
                        *(round(median(a[2][s] for a in last), 1) for s in STATS))
    con.executemany("INSERT OR REPLACE INTO pairs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", prows)
    con.executemany("INSERT OR REPLACE INTO baselines VALUES (?,?,?,?,?,?,?,?,?,?)",
                    list(brows.values()))
    con.commit()
    con.close()
    print(f"nba_wowy.sqlite: {len(prows)} pair priors · {len(brows)} baselines "
          f"(trailing {POOL_DAYS}d through {end})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--build", action="store_true")
    a = ap.parse_args()
    if a.validate:
        validate()
    if a.build:
        build()
