"""NBA port · Phase 0 — the beneficiary backtest (walk-forward, prior-only).

Validates the WNBA injury-beneficiary engine's core premise on NBA data before any build:
when a >=28-min player misses a game, can we PREDICT which teammates get a real bump —
and do the WNBA bands (predicted d_min 3-8 = sweet spot; <3 noise; 8+ over-reach) hold?

Design (mirrors the validated WNBA lighter-signals backtest):
  ABSENCE   X averaged >=28 min over his last 10 played games, played within the last
            12 days (active, not traded/long-term), and does not appear in this game.
  BENEFICIARY  teammate B who plays this game, >=5 prior appearances, and >=2 PRIOR
            games this season with X out (the WOWY prior — "confirmed role" analog).
  PREDICT   d_min = avg_min(B | X out, prior) - avg_min(B | X in, prior)
            proj  = per-min PRA rate (prior X-out games) * (baseline_min + d_min)
  LINE PROXY  B's trailing-10 median PRA (books anchor lines near recent production).
  GRADE     realized PRA > proxy line?  Report hit% by predicted-d_min band and by
            absence-game index (1st game of an absence = the news edge; later games =
            books have adjusted).

stats.nba.com only works from residential IPs (Mac) — run fetch here, never on CI/VM.
    python nba_beneficiary_backtest.py --fetch     # pull 4 seasons into nba_gamelogs.sqlite
    python nba_beneficiary_backtest.py             # run the backtest + report
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
DB = HERE / "nba_gamelogs.sqlite"          # local-only (gitignored): ~100k rows, rebuildable
SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26"]

STAR_MIN = 28.0        # absence trigger: X's trailing-10 avg minutes
ACTIVE_DAYS = 12       # X must have played within this window (not traded/season-ending)
MIN_PRIOR_APPS = 5     # B needs this many prior games for a baseline
MIN_WOWY = 2           # B needs this many prior X-out games for the WOWY prior


def fetch():
    from curl_cffi import requests as cr
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS logs (
        season TEXT, game_id TEXT, date TEXT, team TEXT, player_id INTEGER,
        player TEXT, min REAL, pts INTEGER, reb INTEGER, ast INTEGER,
        PRIMARY KEY (game_id, player_id))""")
    H = {"Referer": "https://www.nba.com/", "Origin": "https://www.nba.com",
         "Accept": "application/json", "x-nba-stats-origin": "stats",
         "x-nba-stats-token": "true"}
    for season in SEASONS:
        url = ("https://stats.nba.com/stats/leaguegamelog?Counter=1000&DateFrom=&DateTo="
               f"&Direction=DESC&LeagueID=00&PlayerOrTeam=P&Season={season}"
               "&SeasonType=Regular%20Season&Sorter=DATE")
        j = cr.get(url, impersonate="chrome", timeout=60, headers=H).json()
        rs = j["resultSets"][0]
        idx = {c: i for i, c in enumerate(rs["headers"])}
        rows = []
        for r in rs["rowSet"]:
            rows.append((season, r[idx["GAME_ID"]], r[idx["GAME_DATE"]],
                         r[idx["TEAM_ABBREVIATION"]], r[idx["PLAYER_ID"]],
                         r[idx["PLAYER_NAME"]], float(r[idx["MIN"]] or 0),
                         int(r[idx["PTS"]] or 0), int(r[idx["REB"]] or 0),
                         int(r[idx["AST"]] or 0)))
        con.executemany("INSERT OR IGNORE INTO logs VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        print(f"{season}: +{len(rows)} player-game rows")
    con.close()


def run():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT season, game_id, date, team, player_id, player, min, "
                       "pts+reb+ast FROM logs ORDER BY date, game_id").fetchall()
    con.close()
    import datetime as dt

    def d(s):
        return dt.date.fromisoformat(s)

    # per (season, team): ordered list of (date, game_id, {pid: (min, pra)})
    team_games = defaultdict(list)
    names = {}
    cur = {}
    for season, gid, date, team, pid, player, mn, pra in rows:
        key = (season, team, gid, date)
        if key not in cur:
            cur[key] = {}
            team_games[(season, team)].append((date, gid, cur[key]))
        cur[key][pid] = (mn, pra)
        names[pid] = player

    bands = defaultdict(lambda: [0, 0])          # band -> [overs, n]
    by_idx = defaultdict(lambda: [0, 0])         # absence-game index -> [overs, n]
    by_margin = defaultdict(lambda: [0, 0])      # proj-line margin band
    control = [0, 0]                             # every qualified beneficiary, no prediction
    flags = 0

    for (season, team), games in team_games.items():
        games.sort(key=lambda g: g[0])
        # walk forward
        for i, (date, gid, box) in enumerate(games):
            if i < 12:
                continue
            prior = games[:i]
            # who's ABSENT: played >=28 min avg over last 10 apps, active within 12 days
            played_by = defaultdict(list)        # pid -> list of (date, min, pra) prior
            for pd, pg, pbox in prior:
                for pid, (mn, pra) in pbox.items():
                    played_by[pid].append((pd, mn, pra))
            absents = []
            for pid, apps in played_by.items():
                if pid in box or len(apps) < 10:
                    continue
                last10 = apps[-10:]
                if (sum(a[1] for a in last10) / 10 >= STAR_MIN
                        and (d(date) - d(apps[-1][0])).days <= ACTIVE_DAYS):
                    absents.append(pid)
            if not absents:
                continue
            X = max(absents, key=lambda p: sum(a[1] for a in played_by[p][-10:]))
            # absence-game index: consecutive team games X has now missed
            aidx = 1
            for pd, pg, pbox in reversed(prior):
                if X in pbox:
                    break
                aidx += 1
            # prior split for WOWY
            out_games = [(pd, pg, pbox) for pd, pg, pbox in prior if X not in pbox]
            in_games = [(pd, pg, pbox) for pd, pg, pbox in prior if X in pbox]
            for B, (bmn, bpra) in box.items():
                if B == X:
                    continue
                apps = played_by.get(B, [])
                if len(apps) < MIN_PRIOR_APPS:
                    continue
                b_out = [(pbox[B][0], pbox[B][1]) for pd, pg, pbox in out_games if B in pbox]
                b_in = [(pbox[B][0], pbox[B][1]) for pd, pg, pbox in in_games if B in pbox]
                if len(b_out) < MIN_WOWY or len(b_in) < 3:
                    continue
                base_min = sum(m for _, m in [(0, a[1]) for a in apps[-10:]]) / min(10, len(apps))
                d_min = (sum(m for m, _ in b_out) / len(b_out)
                         - sum(m for m, _ in b_in) / len(b_in))
                out_rate = (sum(p for _, p in b_out) / max(1e-9, sum(m for m, _ in b_out)))
                proj = out_rate * (base_min + d_min)
                line = median(a[2] for a in apps[-10:])           # trailing-10 median PRA
                over = 1 if bpra > line else 0
                control[0] += over; control[1] += 1
                if proj <= line:                                   # engine says no bet
                    continue
                band = ("<3" if d_min < 3 else "3-8" if d_min <= 8 else "8+")
                bands[band][0] += over; bands[band][1] += 1
                by_idx[min(aidx, 4)][0] += over; by_idx[min(aidx, 4)][1] += 1
                m = proj - line
                mb = ("0-2" if m < 2 else "2-5" if m < 5 else "5+")
                by_margin[mb][0] += over; by_margin[mb][1] += 1
                flags += 1

    print(f"=== NBA beneficiary backtest — {len(SEASONS)} seasons, walk-forward ===")
    print(f"control (all qualified beneficiaries, no engine): "
          f"{control[0]}/{control[1]} = {control[0]/max(1,control[1])*100:.1f}% over proxy line")
    print(f"engine flags (proj > line): {flags}\n")
    print("by PREDICTED d_min band (the WNBA bands were <3:46% · 3-8:76% · 8+:12%):")
    for b in ("<3", "3-8", "8+"):
        o, n = bands[b]
        print(f"  d_min {b:<4} {o:>5}/{n:<5} = {o/max(1,n)*100:5.1f}%")
    print("\nby absence-game index (1 = first game X misses — the NEWS edge):")
    for k in sorted(by_idx):
        o, n = by_idx[k]
        lbl = f"{k}" if k < 4 else "4+"
        print(f"  game {lbl:<3} {o:>5}/{n:<5} = {o/max(1,n)*100:5.1f}%")
    print("\nby proj-line margin:")
    for b in ("0-2", "2-5", "5+"):
        o, n = by_margin[b]
        print(f"  +{b:<4} {o:>5}/{n:<5} = {o/max(1,n)*100:5.1f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        fetch()
    else:
        run()
