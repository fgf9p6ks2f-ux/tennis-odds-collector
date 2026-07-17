"""NBA port — SLATE POLICY backtest: 1 vs 2-disjoint vs 3-pure plays per team-game.

User question (2026-07-17): when the engine flags multiple same-team plays in a game,
is it optimal to bet 1 play, 2 plays whose props share NO component (over pts + over ast
fine; over pts + over P+A not), or the only allowed 3-way (pure pts + reb + ast singles)?

Walk-forward on the Phase-0 skeleton (nba_beneficiary_backtest.py), extended per-stat:
  stats: pts / reb / ast singles + pr / pa / ra / pra combos
  proj_s = prior X-out per-min rate of s * (base_min + d_min);  line_s = trailing-10 median
  candidate = flag when margin_s = proj_s - line_s >= per-stat threshold
              (pts 3 / reb 2 / ast 2 / pr 4 / pa 4 / ra 3 / pra 5 — production-scale)
  PRODUCTION CELL only: absence game index 1-2 (the news window we actually bet).
  Rank candidates within a team-game by margin/threshold (cross-stat comparable).

Policies compared on identical candidate pools:
  P1          top-1
  P2-any      top-2, overlap allowed (naive "take two")
  P2-disjoint top-1 + next candidate with NO shared P/R/A component (the user's rule)
  P3-pure     up to pts + reb + ast singles, all disjoint by construction

Report: per-policy n / hit% / ROI at -110 flat, PER-SLOT marginal hit% (is the 2nd/3rd
play still above the 52.4% break-even?), and units per team-game (volume-adjusted).
Pushes (realized == line) are excluded like the ledger does. Proxy-line caveat as Phase 0:
relative policy comparison on the same pool is the point, not absolute hit rates.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
DB = HERE / "nba_gamelogs.sqlite"

STAR_MIN = 28.0
ACTIVE_DAYS = 12
MIN_PRIOR_APPS = 5
MIN_WOWY = 2
NEWS_WINDOW = 2                      # absence game 1-2 = the validated production cell

# stat -> (component set, flag threshold on proj-line margin)
STATS = {
    "pts": ({"P"}, 3.0), "reb": ({"R"}, 2.0), "ast": ({"A"}, 2.0),
    "pr": ({"P", "R"}, 4.0), "pa": ({"P", "A"}, 4.0), "ra": ({"R", "A"}, 3.0),
    "pra": ({"P", "R", "A"}, 5.0),
}
PAYOUT = 100 / 110                   # flat 1u at -110


def val(t, stat):                    # t = (pts, reb, ast)
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

    pools = []                        # one entry per team-game: list of candidate dicts
    for (season, team), games in team_games.items():
        games.sort(key=lambda g: g[0])
        for i, (date, gid, box) in enumerate(games):
            if i < 12:
                continue
            prior = games[:i]
            played_by = defaultdict(list)   # pid -> [(date, min, (p,r,a))]
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
            if aidx > NEWS_WINDOW:            # production cell only
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
                    line = median(val(t, stat) for _, _, t in
                                  [(0, 0, x[2]) for x in apps[-10:]])
                    m = proj - line
                    if m < thr:
                        continue
                    realized = val(bt, stat)
                    if realized == line:              # push — excluded from records
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
    print(f"team-game candidate pools: {len(pools)} · "
          f"pools with 2+ candidates: {sum(1 for p in pools if len(p) > 1)} · "
          f"avg pool size {sum(len(p) for p in pools)/max(1,len(pools)):.1f}")
    print(f"break-even at -110 = 52.4%\n")
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
        roi = units / max(1, n) * 100
        print(f"{name:<26}{n:>5}{wins/max(1,n)*100:>6.1f}%{roi:>7.1f}%"
              f"{units/max(1,len(pools)):>13.3f}")
        for j in sorted(slot):
            o, m = slot[j]
            print(f"    slot {j+1}: {o}/{m} = {o/max(1,m)*100:.1f}%")
    # the marginal question directly: among pools where a disjoint 2nd exists,
    # does ADDING it beat betting the top-1 alone (per team-game units)?
    both = [p for p in pools if len(pick_disjoint(p, 2)) == 2]
    u1 = sum((PAYOUT if p[0]["hit"] else -1.0) for p in both)
    u2 = sum(sum(PAYOUT if c["hit"] else -1.0 for c in pick_disjoint(p, 2)) for p in both)
    print(f"\npools where a disjoint 2nd play exists: {len(both)}")
    print(f"  betting top-1 only:      {u1:+.1f}u ({u1/max(1,len(both)):+.3f}/pool)")
    print(f"  betting both (disjoint): {u2:+.1f}u ({u2/max(1,len(both)):+.3f}/pool)")


if __name__ == "__main__":
    apply_policies(candidates_by_teamgame())
