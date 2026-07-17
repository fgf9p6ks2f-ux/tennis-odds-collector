"""WNBA FRONTIER backtest — the signal classes the live bot does NOT bet (2026-07-17 audit).

User: "is this the optimal model? investigate anything it missed." Walk-forward on the same
6-season store (wnba_gamelogs.sqlite) as the slate-policy backtest, absence engine identical
(trailing-10 >=25 min player missing, active +/-12d). Proxy line = trailing-10 median (same
epistemics as Phase 0: relative signal strength, not absolute hit rates).

Tested frontiers:
  A. RETURN-GAME UNDERS (the mirror edge): X returns after missing >=2 team games -> the
     teammates whose role was elevated during the absence keep stale-HIGH anchors; bet their
     UNDER on the return night. (Distinct from the dropped error-line unders.)
  B. OPPONENT-SIDE OVERS: X out -> do OPPONENT rotation players beat their medians (softer
     matchups), and which stat carries it?
  C. VACUUM SIZE: same-team beneficiary cell split by 1 vs 2+ qualifying absences.
  D. B2B FATIGUE: beneficiary overs split by whether the team played the previous day.

    python3 wnba_frontier_backtest.py
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
DB = HERE / "wnba_gamelogs.sqlite"

STAR_MIN = 25.0
ACTIVE_DAYS = 12
WARMUP = 10
STATS = ("pts", "reb", "ast", "pra")


def val(t, s):
    p, r, a = t
    return {"pts": p, "reb": r, "ast": a, "pra": p + r + a}[s]


def load():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT season, game_id, date, team, player_id, min, pts, reb, ast "
                       "FROM logs ORDER BY date, game_id").fetchall()
    con.close()
    team_games = defaultdict(list)
    game_teams = defaultdict(set)                      # game_id -> the two teams
    cur = {}
    for season, gid, date, team, pid, mn, p, r, a in rows:
        key = (season, team, gid, date)
        if key not in cur:
            cur[key] = {}
            team_games[(season, team)].append((date, gid, cur[key]))
        cur[key][pid] = (mn, (p, r, a))
        game_teams[(season, gid)].add(team)
    return team_games, game_teams


def run():
    import datetime as dt

    def d(s):
        return dt.date.fromisoformat(s)

    team_games, game_teams = load()
    boxes = {}                                          # (season, team, gid) -> box
    for (season, team), games in team_games.items():
        for date, gid, box in games:
            boxes[(season, team, gid)] = box

    ret_under = defaultdict(lambda: [0, 0])             # A: stat -> [unders, n]
    opp_over = defaultdict(lambda: [0, 0])              # B: stat -> [overs, n]
    vac_over = defaultdict(lambda: [0, 0])              # C: n_absent-bucket -> [overs, n]
    b2b_over = defaultdict(lambda: [0, 0])              # D: b2b?  -> [overs, n]

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

            def is_star(pid):
                apps = played_by.get(pid, [])
                return (len(apps) >= 10 and sum(x[1] for x in apps[-10:]) / 10 >= STAR_MIN)

            absents = [pid for pid, apps in played_by.items()
                       if pid not in box and is_star(pid)
                       and (d(date) - d(apps[-1][0])).days <= ACTIVE_DAYS]

            # ── A. RETURN GAME: a star IS in tonight's box after missing the last 2+ games ──
            for pid in box:
                if not is_star(pid):
                    continue
                missed = 0
                for pd_, pg_, pbox_ in reversed(prior):
                    if pid in pbox_:
                        break
                    missed += 1
                if missed < 2:
                    continue
                absent_gids = {pg_ for pd_, pg_, pbox_ in prior[-missed:]}
                for B, (bmn, bt) in box.items():
                    if B == pid or len(played_by.get(B, [])) < 8:
                        continue
                    # elevated during the absence: played those games at >=18 min avg
                    inabs = [(m, t) for pd_, pg_, pbox_ in prior[-missed:]
                             for bb, (m, t) in pbox_.items() if bb == B]
                    if len(inabs) < 2 or sum(m for m, _ in inabs) / len(inabs) < 18:
                        continue
                    last10 = played_by[B][-10:]
                    for s in STATS:
                        line = median(val(x[2], s) for x in last10)
                        realized = val(bt, s)
                        if realized == line:
                            continue
                        ret_under[s][0] += (realized < line)
                        ret_under[s][1] += 1

            if not absents:
                continue
            X = max(absents, key=lambda p: sum(x[1] for x in played_by[p][-10:]))
            aidx = 1
            for pd_, pg_, pbox_ in reversed(prior):
                if X in pbox_:
                    break
                aidx += 1
            if aidx > 2:                                # news window, same as production
                continue

            # ── B. OPPONENT side: opponent rotation players vs their own medians ──
            opp = next((t for t in game_teams[(season, gid)] if t != team), None)
            obox = boxes.get((season, opp, gid)) if opp else None
            if obox:
                ogames = team_games.get((season, opp), [])
                oprior = [g for g in ogames if g[0] < date]
                opl = defaultdict(list)
                for pd_, pg_, pbox_ in oprior:
                    for pid2, (mn2, t2) in pbox_.items():
                        opl[pid2].append((pd_, mn2, t2))
                for O, (omn, ot) in obox.items():
                    apps = opl.get(O, [])
                    if len(apps) < 8:
                        continue
                    last10 = apps[-10:]
                    if sum(x[1] for x in last10) / len(last10) < 15:
                        continue
                    for s in STATS:
                        line = median(val(x[2], s) for x in last10)
                        realized = val(ot, s)
                        if realized == line:
                            continue
                        opp_over[s][0] += (realized > line)
                        opp_over[s][1] += 1

            # ── C+D. same-team beneficiary cell (margin >=2 pra proxy), by vacuum + b2b ──
            out_games = [pbox_ for _, _, pbox_ in prior if X not in pbox_]
            in_games = [pbox_ for _, _, pbox_ in prior if X in pbox_]
            b2b = (d(date) - d(prior[-1][0])).days == 1 if prior else False
            for B, (bmn, bt) in box.items():
                if B == X or len(played_by.get(B, [])) < 5:
                    continue
                b_out = [pbox_[B] for pbox_ in out_games if B in pbox_]
                b_in = [pbox_[B] for pbox_ in in_games if B in pbox_]
                if len(b_out) < 2 or len(b_in) < 3:
                    continue
                base_min = sum(x[1] for x in played_by[B][-10:]) / min(10, len(played_by[B]))
                d_min = (sum(m for m, _ in b_out) / len(b_out)
                         - sum(m for m, _ in b_in) / len(b_in))
                rate = sum(val(t, "pra") for _, t in b_out) / max(1e-9, sum(m for m, _ in b_out))
                proj = rate * (base_min + d_min)
                line = median(val(x[2], "pra") for x in played_by[B][-10:])
                if proj - line < 4.0:
                    continue
                realized = val(bt, "pra")
                if realized == line:
                    continue
                over = realized > line
                vac_over["1 out" if len(absents) == 1 else "2+ out"][0] += over
                vac_over["1 out" if len(absents) == 1 else "2+ out"][1] += 1
                b2b_over["b2b" if b2b else "rested"][0] += over
                b2b_over["b2b" if b2b else "rested"][1] += 1

    def show(title, dd, side="over"):
        print(f"\n── {title} ──")
        for k in sorted(dd):
            h, n = dd[k]
            if n >= 15:
                print(f"  {k:<8} {h}/{n} = {h/n*100:.1f}% {side}")

    show("A. RETURN-game teammate UNDERS (vs trailing-10 median)", ret_under, "under")
    show("B. OPPONENT players vs own median when a star is out", opp_over, "over")
    show("C. beneficiary cell by VACUUM size", vac_over)
    show("D. beneficiary cell by rest", b2b_over)


if __name__ == "__main__":
    run()
