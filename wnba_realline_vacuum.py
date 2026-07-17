"""Vacuum-size split at REAL logged FanDuel/DK lines (user rule 2026-07-17: no proxy lines).

For every 2026 team-date since line-logging began (7/7): detect qualifying absences from the
game logs (trailing-10 >=25 min star, active within 12d, team played without her), then grade
every lined TEAMMATE's earliest-posted main line (rung with odds nearest even at the first
collection of the day) against what actually happened. Units at the REAL paired odds.
Splits: control (no absence) vs 1 star out vs 2+ stars out.
"""
import sqlite3, unicodedata, datetime as dt
from collections import defaultdict
from statistics import median

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower().strip()

VAL = {"points": lambda p,r,a: p, "rebounds": lambda p,r,a: r, "assists": lambda p,r,a: a,
       "pra": lambda p,r,a: p+r+a, "pts_reb": lambda p,r,a: p+r, "pts_ast": lambda p,r,a: p+a,
       "reb_ast": lambda p,r,a: r+a}

g = sqlite3.connect("wnba_gamelogs.sqlite")
rows = g.execute("SELECT date, team, player_id, player, min, pts, reb, ast FROM logs "
                 "WHERE season='2026' ORDER BY date").fetchall()
team_dates = defaultdict(dict)                    # (date, team) -> {pid: (name, min, p, r, a)}
hist = defaultdict(list)                          # pid -> [(date, min)]
names = {}
for date, team, pid, player, mn, p, r, a in rows:
    team_dates[(date, team)][pid] = (player, mn, p, r, a)
    hist[pid].append((date, mn))
    names[pid] = player
team_of = defaultdict(set)
for (date, team), box in team_dates.items():
    for pid in box:
        team_of[pid].add((date, team))

def absences(date, team, box):
    """qualifying stars missing this team-date"""
    out = []
    # players seen on this team before `date`
    seen = {pid for (d2, t2), b in team_dates.items() if t2 == team and d2 < date for pid in b}
    for pid in seen:
        if pid in box:
            continue
        apps = [(d2, m) for d2, m in hist[pid] if d2 < date]
        if len(apps) < 10:
            continue
        last10 = apps[-10:]
        if sum(m for _, m in last10) / 10 < 25.0:
            continue
        if (dt.date.fromisoformat(date) - dt.date.fromisoformat(apps[-1][0])).days > 12:
            continue
        out.append(pid)
    return out

L = sqlite3.connect("fanduel_props.sqlite")
lines = L.execute("SELECT DATE(collected_at), player, stat, line, side, odds, collected_at "
                  "FROM fd_lines WHERE sport='wnba' AND side='over' AND odds IS NOT NULL "
                  "ORDER BY collected_at").fetchall()
first_snap = {}                                    # (date, nplayer, stat) -> {line: odds} at first ts
first_ts = {}
for d_, pl, stt, ln, sd, od, ca in lines:
    k = (d_, norm(pl), stt)
    if k not in first_ts:
        first_ts[k] = ca[:16]                      # minute resolution of the first collection
        first_snap[k] = {}
    if ca[:16] == first_ts[k]:
        first_snap[k][ln] = od

res = defaultdict(lambda: [0, 0, 0.0])             # bucket -> [wins, n, units]
detail = defaultdict(list)
for (date, team), box in sorted(team_dates.items()):
    if date < "2026-07-07":
        continue
    ab = absences(date, team, box)
    bucket = "control" if not ab else ("1 out" if len(ab) == 1 else "2+ out")
    for pid, (player, mn, p, r, a) in box.items():
        for stat, fn in VAL.items():
            k = (date, norm(player), stat)
            snap = first_snap.get(k)
            if not snap:
                continue
            # main line = rung with odds nearest even at first collection
            ln = min(snap, key=lambda x: abs(snap[x] - 1.91))
            od = snap[ln]
            actual = fn(p, r, a)
            if actual == ln:
                continue
            w = actual > ln
            res[bucket][0] += w
            res[bucket][1] += 1
            res[bucket][2] += (od - 1) if w else -1.0
            if bucket == "2+ out":
                detail[(date, team)].append((player, stat, ln, round(od,2), actual, "W" if w else "L"))

print("═══ REAL-LINE vacuum split (earliest posted line each day, real odds) ═══")
for b in ("control", "1 out", "2+ out"):
    w, n, u = res[b]
    if n:
        print(f"  {b:<8} {w}/{n} = {w/n*100:.1f}% over · {u:+.1f}u at real odds ({u/n*100:+.0f}% ROI)")
print("\n2+-out team-dates and their lined teammates:")
for (date, team), plays in sorted(detail.items()):
    outs = [names[pid] for pid in absences(date, team, team_dates[(date, team)])]
    print(f"  {date} {team} (out: {', '.join(outs)}) — {len(plays)} lines")
