"""DEEP opposing-bigs validation (task 2026-07-19) — narrowing the surviving signal.

Prototype verdict: pts effect fails the placebo (guards gain the same), bigs-REB is the
only differentiated lift (+2.5pt over-rate vs trailing-10 median, placebo -0.1). This
script stress-tests that survivor:
  1. per-season stability (2021-2026) — does the reb lift persist or is it one hot year?
  2. role split — starter bigs (trailing-10 >= 22 min) vs bench bigs
  3. magnitude — median rebound delta vs own trailing-10 median
  4. center-quality gate sensitivity (out center trailing-10 >= 25 vs >= 28 min)
  5. REAL LINES: fd_lines since 7/7 — opposing bigs' posted REB lines on center-out days,
     graded vs actuals (the only evidence class that ships anything).
"""
import datetime as dt
import json
import sqlite3
from collections import defaultdict
from statistics import median

HERE = __file__.rsplit("/", 1)[0]
POS = {int(k): v for k, v in json.load(open(f"{HERE}/wnba_positions.json")).items()}

g = sqlite3.connect(f"{HERE}/wnba_gamelogs.sqlite")
rows = g.execute("SELECT season, date, team, game_id, player_id, player, min, pts, reb "
                 "FROM logs ORDER BY date").fetchall()
g.close()

team_dates = defaultdict(dict)
hist = defaultdict(list)
game_teams = defaultdict(set)
names = {}
for season, date, team, gid, pid, name, mn, p, r in rows:
    team_dates[(date, team, gid)][pid] = (mn, p, r)
    hist[pid].append((date, mn, p, r))
    game_teams[gid].add(team)
    names[pid] = name


def is_big(pid, prior):
    if "C" in POS.get(pid, ""):
        return True
    rr = prior[-10:]
    return bool(rr) and sum(x[3] for x in rr) / len(rr) >= 6.5


def center_out(date, team, gid, box, min_mpg):
    seen = {pid for (d2, t2, g2), b in team_dates.items() if t2 == team and d2 < date for pid in b}
    for pid in seen:
        if pid in box:
            continue
        apps = [(d2, m) for d2, m, _, _ in hist[pid] if d2 < date]
        if len(apps) < 10 or sum(m for _, m in apps[-10:]) / 10 < min_mpg:
            continue
        if (dt.date.fromisoformat(date) - dt.date.fromisoformat(apps[-1][0])).days > 12:
            continue
        if "C" in POS.get(pid, ""):
            return True
    return False


def run(min_center_mpg):
    per_season = defaultdict(lambda: [0, 0])
    per_season_ctl = defaultdict(lambda: [0, 0])
    starter = [0, 0]
    bench = [0, 0]
    deltas, deltas_ctl = [], []
    for (date, team, gid), box in sorted(team_dates.items()):
        opp = next((t for t in game_teams[gid] if t != team), None)
        obox = team_dates.get((date, opp, gid)) if opp else None
        if not obox:
            continue
        treat = center_out(date, team, gid, box, min_center_mpg)
        for pid, (mn, p, r) in obox.items():
            prior = [x for x in hist[pid] if x[0] < date]
            if len(prior) < 8 or not is_big(pid, prior):
                continue
            med = median(x[3] for x in prior[-10:])
            over = r > med
            season = date[:4]
            if treat:
                per_season[season][0] += over
                per_season[season][1] += 1
                role = sum(x[1] for x in prior[-10:]) / 10
                tgt = starter if role >= 22 else bench
                tgt[0] += over
                tgt[1] += 1
                deltas.append(r - med)
            else:
                per_season_ctl[season][0] += over
                per_season_ctl[season][1] += 1
                deltas_ctl.append(r - med)
    print(f"\n== center gate: trailing-10 >= {min_center_mpg} min (treat vs CONTROL) ==")
    for s in sorted(per_season):
        w, n = per_season[s]
        cw, cn = per_season_ctl.get(s, (0, 0))
        ctl = f"{cw/cn*100:.1f}%" if cn else "—"
        lift = f"{(w/n - cw/cn)*100:+.1f}" if cn else ""
        print(f"  {s}: out {w}/{n} = {w/n*100:.1f}%  vs ctl {ctl}  {lift}")
    for lab, (w, n) in (("starter bigs (>=22min)", starter), ("bench bigs", bench)):
        if n:
            print(f"  {lab}: {w}/{n} = {w/n*100:.1f}%")
    if deltas and deltas_ctl:
        print(f"  median reb delta: treat {median(deltas):+.1f} (n={len(deltas)}) "
              f"vs ctl {median(deltas_ctl):+.1f} (n={len(deltas_ctl)})")


run(25.0)
run(28.0)

# ── REAL LINES: fd_lines replay on center-out days since 7/7 ──
lc = sqlite3.connect(f"{HERE}/fanduel_props.sqlite")
lc.row_factory = sqlite3.Row
name2pid = {v: k for k, v in names.items()}
res = [0, 0, 0.0]
detail = []
for (date, team, gid), box in sorted(team_dates.items()):
    if date < "2026-07-07":
        continue
    opp = next((t for t in game_teams[gid] if t != team), None)
    obox = team_dates.get((date, opp, gid)) if opp else None
    if not obox or not center_out(date, team, gid, box, 25.0):
        continue
    for pid, (mn, p, r) in obox.items():
        prior = [x for x in hist[pid] if x[0] < date]
        if len(prior) < 8 or not is_big(pid, prior):
            continue
        nm = names.get(pid)
        line = lc.execute(
            "SELECT line, odds FROM fd_lines WHERE book='fd' AND player=? "
            "AND stat='rebounds' AND side='over' AND date(collected_at)=? "
            "ORDER BY ABS(odds - 1.90) LIMIT 1", (nm, date)).fetchone()
        if not line or line["line"] is None:
            continue
        w = r > line["line"]
        if abs(r - line["line"]) < 1e-9:
            continue
        res[0] += w
        res[1] += 1
        res[2] += (float(line["odds"]) - 1) if w else -1.0
        detail.append(f"  {date} {nm}: reb {r:g} vs o{line['line']:g} ({'W' if w else 'L'})")
print(f"\n== REAL FD LINES (center-out days since 7/7): opposing bigs REB overs ==")
for x in detail:
    print(x)
if res[1]:
    print(f"  record {res[0]}-{res[1]-res[0]} ({res[0]/res[1]*100:.0f}%), {res[2]:+.2f}u flat")
else:
    print("  no priced samples")
