"""CENTER-OUT -> OPPOSING BIGS backtest (user hypothesis 2026-07-17: Boston out -> Malonga/Fam eat).

Walk-forward, 6 seasons. When a team's qualifying CENTER (trailing-10 >=25 min, position
contains C) is out, how do the OPPOSING team's position groups do vs their own trailing-10
medians — bigs (C/F-C/C-F) vs pure forwards vs guards (the placebo)? Controls = the same
groups on every other slate. Proxy medians = SIGNAL-SCOPING ONLY per the real-lines rule;
any bettable claim needs the fd_lines check that follows.
"""
import sqlite3, json, datetime as dt
from collections import defaultdict
from statistics import median

POS = {int(k): v for k, v in json.load(open("wnba_positions.json")).items()}

def group(pid, reb_rate):
    p = POS.get(pid, "")
    if "C" in p:
        return "big"
    if p == "F":
        return "fwd"
    if "G" in p:
        return "grd"
    return "big" if reb_rate >= 6.0 else "grd"     # heuristic for the unmapped tail

g = sqlite3.connect("wnba_gamelogs.sqlite")
rows = g.execute("SELECT date, team, game_id, player_id, min, pts, reb, ast FROM logs ORDER BY date").fetchall()
team_dates = defaultdict(dict); hist = defaultdict(list); game_teams = defaultdict(set)
for date, team, gid, pid, mn, p, r, a in rows:
    team_dates[(date, team, gid)][pid] = (mn, p, r, a)
    hist[pid].append((date, mn, p, r, a))
    game_teams[gid].add(team)

def qual_center_out(date, team, gid, box):
    seen = {pid for (d2, t2, g2), b in team_dates.items() if t2 == team and d2 < date for pid in b}
    for pid in seen:
        if pid in box:
            continue
        apps = [(d2, m) for d2, m, *_ in hist[pid] if d2 < date]
        if len(apps) < 10 or sum(m for _, m in apps[-10:]) / 10 < 25.0:
            continue
        if (dt.date.fromisoformat(date) - dt.date.fromisoformat(apps[-1][0])).days > 12:
            continue
        rr = [x for x in hist[pid] if x[0] < date][-10:]
        if "C" in POS.get(pid, "") or (sum(x[3] for x in rr) / len(rr) >= 6.5):
            return True
    return False

eff = defaultdict(lambda: [0, 0]); ctl = defaultdict(lambda: [0, 0])
for (date, team, gid), box in sorted(team_dates.items()):
    opp = next((t for t in game_teams[gid] if t != team), None)
    if not opp:
        continue
    obox = team_dates.get((date, opp, gid))
    if not obox:
        continue
    center_out = qual_center_out(date, team, gid, box)
    for pid, (mn, p, r, a) in obox.items():
        prior = [x for x in hist[pid] if x[0] < date]
        if len(prior) < 8:
            continue
        last10 = prior[-10:]
        if sum(x[1] for x in last10) / len(last10) < 15:
            continue
        gr = group(pid, sum(x[3] for x in last10) / len(last10))
        for stat, ix in (("pts", 2), ("reb", 3)):
            line = median(x[ix] for x in last10)
            actual = (p, r)[ix - 2]
            if actual == line:
                continue
            d_ = eff if center_out else ctl
            d_[(gr, stat)][0] += actual > line
            d_[(gr, stat)][1] += 1

print("opposing-team splits vs own trailing-10 median (over rate):")
print(f"{'group':<12}{'CENTER OUT':>16}{'control':>16}{'lift':>8}")
for gr in ("big", "fwd", "grd"):
    for stat in ("pts", "reb"):
        w, n = eff[(gr, stat)]; cw, cn = ctl[(gr, stat)]
        if n >= 30 and cn:
            print(f"{gr} {stat:<7} {w}/{n} = {w/n*100:5.1f}%   {cw}/{cn} = {cw/cn*100:5.1f}%"
                  f" {(w/n - cw/cn)*100:+6.1f}pt")
