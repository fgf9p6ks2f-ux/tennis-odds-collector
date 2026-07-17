"""WNBA SLATE POLICY backtest — 1 vs 2-disjoint vs 3-pure plays per team-game.

The WNBA answer to the user's question (2026-07-17): when the engine flags multiple
same-team plays in a game, bet 1, bet 2 with NO shared P/R/A component (over pts +
over ast fine; over pts + over P+A not), or the pure 3-way (pts + reb + ast singles)?

Same walk-forward skeleton as nba_slate_policy_backtest.py (Phase-0 family), WNBA-scaled:
  absence trigger trailing-10 >= 25 min (40-minute games), 10-game warmup, measured WOWY
  priors (>=2 prior X-out games — the transferable signal per the NBA port findings),
  proxy line = trailing-10 median, per-stat flag thresholds ~0.7x the NBA ones.
  PRODUCTION CELL only: absence game 1-2. Rank candidates by margin/threshold.

stats.nba.com is Mac-only (blocks CI/datacenter):
    python3 wnba_slate_policy_backtest.py --fetch   # build wnba_gamelogs.sqlite (gitignored)
    python3 wnba_slate_policy_backtest.py           # run the policy comparison
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_gamelogs.sqlite"           # local-only, rebuildable via --fetch
SEASONS = ["2021", "2022", "2023", "2024", "2025", "2026"]

STAR_MIN = 25.0        # absence trigger: trailing-10 avg minutes (40-min WNBA games)
ACTIVE_DAYS = 12
MIN_PRIOR_APPS = 5
MIN_WOWY = 2
WARMUP = 10            # 40-game season (vs 12 for NBA's 82)
NEWS_WINDOW = 2        # absence game 1-2 = the cell we actually bet live

# stat -> (component set, flag threshold on proj-line margin) — WNBA-scaled
STATS = {
    "pts": ({"P"}, 2.5), "reb": ({"R"}, 2.0), "ast": ({"A"}, 1.5),
    "pr": ({"P", "R"}, 3.5), "pa": ({"P", "A"}, 3.5), "ra": ({"R", "A"}, 2.5),
    "pra": ({"P", "R", "A"}, 4.0),
}
PAYOUT = 100 / 110


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
               f"&Direction=DESC&LeagueID=10&PlayerOrTeam=P&Season={season}"
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
        print(f"WNBA {season}: +{len(rows)} player-game rows")
    con.close()


def val(t, stat):
    p, r, a = t
    return {"pts": p, "reb": r, "ast": a, "pr": p + r, "pa": p + a,
            "ra": r + a, "pra": p + r + a}[stat]


def candidates_by_teamgame():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT season, game_id, date, team, player_id, player, min, "
                       "pts, reb, ast FROM logs ORDER BY date, game_id").fetchall()
    con.close()
    import datetime as dt

    def d(s):
        return dt.date.fromisoformat(s)

    team_games = defaultdict(list)
    cur = {}
    for season, gid, date, team, pid, player, mn, p, r, a in rows:
        key = (season, team, gid, date)
        if key not in cur:
            cur[key] = {}
            team_games[(season, team)].append((date, gid, cur[key]))
        cur[key][pid] = (mn, (p, r, a))

    pools = []
    for (season, team), games in team_games.items():
        games.sort(key=lambda g: g[0])
        for i, (date, gid, box) in enumerate(games):
            if i < WARMUP:
                continue
            prior = games[:i]
            played_by = defaultdict(list)
            for pd, pg, pbox in prior:
                for pid, (mn, t) in pbox.items():
                    played_by[pid].append((pd, mn, t))
            absents = []
            for pid, apps in played_by.items():
                if pid in box or len(apps) < 10:
                    continue
                last10 = apps[-10:]
                if (sum(x[1] for x in last10) / 10 >= STAR_MIN
                        and (d(date) - d(apps[-1][0])).days <= ACTIVE_DAYS):
                    absents.append(pid)
            if not absents:
                continue
            X = max(absents, key=lambda p: sum(x[1] for x in played_by[p][-10:]))
            aidx = 1
            for pd, pg, pbox in reversed(prior):
                if X in pbox:
                    break
                aidx += 1
            if aidx > NEWS_WINDOW:
                continue
            out_games = [pbox for _, _, pbox in prior if X not in pbox]
            in_games = [pbox for _, _, pbox in prior if X in pbox]
            pool = []
            for B, (bmn, bt) in box.items():
                if B == X:
                    continue
                apps = played_by.get(B, [])
                if len(apps) < MIN_PRIOR_APPS:
                    continue
                b_out = [pbox[B] for pbox in out_games if B in pbox]
                b_in = [pbox[B] for pbox in in_games if B in pbox]
                if len(b_out) < MIN_WOWY or len(b_in) < 3:
                    continue
                base_min = sum(x[1] for x in apps[-10:]) / min(10, len(apps))
                d_min = (sum(m for m, _ in b_out) / len(b_out)
                         - sum(m for m, _ in b_in) / len(b_in))
                out_min = max(1e-9, sum(m for m, _ in b_out))
                for stat, (comps, thr) in STATS.items():
                    rate = sum(val(t, stat) for _, t in b_out) / out_min
                    proj = rate * (base_min + d_min)
                    line = median(val(x[2], stat) for x in apps[-10:])
                    m = proj - line
                    if m < thr:
                        continue
                    realized = val(bt, stat)
                    if realized == line:          # push — excluded like the ledger
                        continue
                    pool.append({"player": B, "stat": stat, "comps": comps,
                                 "rank": m / thr, "margin": m,
                                 "hit": 1 if realized > line else 0})
            if pool:
                pool.sort(key=lambda c: -c["rank"])
                pools.append(pool)
    return pools


def apply_policies(pools):
    def pick_disjoint(pool, k, pure_only=False):
        picks, used = [], set()
        for c in pool:
            if len(picks) == k:
                break
            if pure_only and len(c["comps"]) > 1:
                continue
            if c["comps"] & used:
                continue
            picks.append(c)
            used |= c["comps"]
        return picks

    policies = {
        "P1 top-1": lambda p: p[:1],
        "P2-any (overlap ok)": lambda p: p[:2],
        "P2-disjoint (user rule)": lambda p: pick_disjoint(p, 2),
        "P3-pure (pts+reb+ast)": lambda p: pick_disjoint(p, 3, pure_only=True),
    }
    print(f"WNBA {SEASONS[0]}-{SEASONS[-1]} · team-game pools: {len(pools)} · "
          f"with 2+ candidates: {sum(1 for p in pools if len(p) > 1)} · "
          f"avg pool {sum(len(p) for p in pools)/max(1,len(pools)):.1f}")
    print("break-even at -110 = 52.4%\n")
    hdr = f"{'policy':<26}{'n':>5}{'hit%':>7}{'ROI':>8}{'u/team-game':>13}"
    print(hdr + "\n" + "-" * len(hdr))
    for name, fn in policies.items():
        slot = defaultdict(lambda: [0, 0])
        wins = n = 0
        units = 0.0
        for pool in pools:
            for j, c in enumerate(fn(pool)):
                n += 1
                wins += c["hit"]
                units += PAYOUT if c["hit"] else -1.0
                slot[j][0] += c["hit"]
                slot[j][1] += 1
        print(f"{name:<26}{n:>5}{wins/max(1,n)*100:>6.1f}%{units/max(1,n)*100:>7.1f}%"
              f"{units/max(1,len(pools)):>13.3f}")
        for j in sorted(slot):
            o, m = slot[j]
            print(f"    slot {j+1}: {o}/{m} = {o/max(1,m)*100:.1f}%")
    both = [p for p in pools if len(pick_disjoint(p, 2)) == 2]
    u1 = sum((PAYOUT if p[0]["hit"] else -1.0) for p in both)
    u2 = sum(sum(PAYOUT if c["hit"] else -1.0 for c in pick_disjoint(p, 2)) for p in both)
    print(f"\npools where a disjoint 2nd play exists: {len(both)}")
    print(f"  top-1 only:              {u1:+.1f}u ({u1/max(1,len(both)):+.3f}/pool)")
    print(f"  both plays (disjoint):   {u2:+.1f}u ({u2/max(1,len(both)):+.3f}/pool)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        fetch()
    else:
        apply_policies(candidates_by_teamgame())
