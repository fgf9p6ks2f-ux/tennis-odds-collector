"""REAL-LINE re-validation of every backtested rule (user rule 2026-07-17: no proxy lines).

Replay: every 2026 team-date since line-logging began (7/7) with a qualifying absence
(trailing-10 >=25 min star, active 12d). For each teammate with a REAL logged line that day:
WOWY vs the primary out star from the game logs (d_min, n_out sample, per-stat out-rate),
projection = rate * (base_min + d_min), margin = proj - the REAL earliest-posted main line.
Grade at that line + its real odds. Then split by every rule the bot uses.
"""
import sqlite3, unicodedata, datetime as dt
from collections import defaultdict

def norm(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()

VAL = {"points": lambda p,r,a: p, "rebounds": lambda p,r,a: r, "assists": lambda p,r,a: a,
       "pra": lambda p,r,a: p+r+a, "pts_reb": lambda p,r,a: p+r, "pts_ast": lambda p,r,a: p+a,
       "reb_ast": lambda p,r,a: r+a}

g = sqlite3.connect("wnba_gamelogs.sqlite")
rows = g.execute("SELECT date, team, player_id, player, min, pts, reb, ast FROM logs "
                 "WHERE season='2026' ORDER BY date").fetchall()
team_dates = defaultdict(dict)
hist = defaultdict(list)                            # pid -> [(date, min, (p,r,a))]
names = {}
for date, team, pid, player, mn, p, r, a in rows:
    team_dates[(date, team)][pid] = (player, mn, p, r, a)
    hist[pid].append((date, mn, (p, r, a)))
    names[pid] = player

L = sqlite3.connect("fanduel_props.sqlite")
lines = L.execute("SELECT DATE(collected_at), player, stat, line, odds, collected_at "
                  "FROM fd_lines WHERE sport='wnba' AND side='over' AND odds IS NOT NULL "
                  "ORDER BY collected_at").fetchall()
first_snap, first_ts = {}, {}
for d_, pl, stt, ln, od, ca in lines:
    k = (d_, norm(pl), stt)
    if k not in first_ts:
        first_ts[k] = ca[:16]
        first_snap[k] = {}
    if ca[:16] == first_ts[k]:
        first_snap[k][ln] = od

def absences(date, team, box):
    seen = {pid for (d2, t2), b in team_dates.items() if t2 == team and d2 < date for pid in b}
    out = []
    for pid in seen:
        if pid in box:
            continue
        apps = [(d2, m) for d2, m, _ in hist[pid] if d2 < date]
        if len(apps) < 10 or sum(m for _, m in apps[-10:]) / 10 < 25.0:
            continue
        if (dt.date.fromisoformat(date) - dt.date.fromisoformat(apps[-1][0])).days > 12:
            continue
        out.append(pid)
    return out

samples = []
for (date, team), box in sorted(team_dates.items()):
    if date < "2026-07-07":
        continue
    ab = absences(date, team, box)
    if not ab:
        continue
    X = max(ab, key=lambda p: sum(m for _, m, _ in hist[p][-10:]))
    x_dates = {d2 for d2, m, _ in hist[X]}
    for pid, (player, mn, p, r, a) in box.items():
        if pid in ab or pid == X:
            continue
        prior = [(d2, m, t) for d2, m, t in hist[pid] if d2 < date]
        if len(prior) < 5:
            continue
        b_out = [(m, t) for d2, m, t in prior if d2 not in x_dates]
        b_in = [(m, t) for d2, m, t in prior if d2 in x_dates]
        base_min = sum(m for _, m, _ in prior[-10:]) / min(10, len(prior))
        n_out = len(b_out)
        d_min = ((sum(m for m, _ in b_out) / n_out) - (sum(m for m, _ in b_in) / len(b_in))
                 if n_out >= 2 and len(b_in) >= 3 else None)
        out_min = max(1e-9, sum(m for m, _ in b_out))
        for stat, fn in VAL.items():
            k = (date, norm(player), stat)
            snap = first_snap.get(k)
            if not snap:
                continue
            ln = min(snap, key=lambda x: abs(snap[x] - 1.91))
            od = snap[ln]
            actual = fn(p, r, a)
            if actual == ln:
                continue
            proj = (sum(fn(*t) for _, t in b_out) / out_min * (base_min + d_min)
                    if d_min is not None else None)
            samples.append({"stat": stat, "d_min": d_min, "n_out": n_out,
                            "margin": (proj - ln) if proj is not None else None,
                            "cold": n_out < 2, "line": ln, "odds": od,
                            "win": actual > ln})

def show(title, rows_):
    w = sum(1 for s in rows_ if s["win"]); n = len(rows_)
    u = sum((s["odds"] - 1) if s["win"] else -1.0 for s in rows_)
    if n >= 10:
        print(f"  {title:<26} {w}/{n} = {w/n*100:4.1f}%  {u:+6.1f}u ({u/n*100:+4.0f}% ROI)")
    elif n:
        print(f"  {title:<26} {w}/{n} (too thin)")

print(f"real-line replay: {len(samples)} lined teammate-stat samples on absence slates\n")
print("── d_min bands (WOWY-measured, all stats) ──")
for lo, hi, lb in ((-99, 0, "<0"), (0, 3, "0-3"), (3, 8, "3-8 (the live band)"), (8, 99, "8+")):
    show(f"d_min {lb}", [s for s in samples if s["d_min"] is not None and lo <= s["d_min"] < hi])
print("── projection margin vs REAL line (flag threshold) ──")
for lo, hi, lb in ((-99, 0, "<0 (no flag)"), (0, 2, "0-2"), (2, 5, "2-5"), (5, 99, "5+ (flag zone)")):
    show(f"margin {lb}", [s for s in samples if s["margin"] is not None and lo <= s["margin"] < hi])
print("── per stat (margin >= 2 only, the flaggable set) ──")
for st in VAL:
    show(st, [s for s in samples if s["stat"] == st and (s["margin"] or -9) >= 2])
print("── thin-sample rule (n_out < 7 & d_min > 10 = the Ayayi guard) ──")
show("guard zone", [s for s in samples if s["n_out"] < 7 and (s["d_min"] or 0) > 10])
show("guard + margin>=5 exempt", [s for s in samples if s["n_out"] < 7 and (s["d_min"] or 0) > 10 and (s["margin"] or 0) >= 5])
print("── cold-start (n_out < 2, no WOWY) ──")
show("cold, all", [s for s in samples if s["cold"]])
