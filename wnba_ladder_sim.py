"""Ladder + parlay construction simulator at REAL logged alt lines & odds.

For every SELECTED graded over (the tracked picks), pull the full real alt ladder FanDuel
posted that day (earliest snapshot per rung) and grade every rung vs what happened. Then
score stake constructions per unit risked. Parlays: every rule-legal combo of the day's
selected base legs at real multiplied odds, by leg count and leg-selection policy.
"""
import sqlite3, unicodedata
from collections import defaultdict
from itertools import combinations

def norm(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()

GAP = {"points": 2.0, "pra": 2.0, "pts_reb": 2.0, "pts_ast": 2.0}

con = sqlite3.connect("wnba_ledger.sqlite"); con.row_factory = sqlite3.Row
g = [dict(r) for r in con.execute("SELECT * FROM predictions WHERE result IN ('over','under') "
                                  "AND (side IS NULL OR side='over')")]
import wnba_slip as S
sel, _ = S.current_selection(list(g))
# one play per (date, player, stat): keep base (lowest line) row; actual from ledger
plays = {}
for r in sel:
    k = (r["pred_date"], r["player"], r["stat"])
    if k not in plays or r["line"] < plays[k]["line"]:
        plays[k] = r

L = sqlite3.connect("fanduel_props.sqlite")
def real_ladder(date, player, stat, base):
    rows = L.execute("SELECT line, odds, collected_at FROM fd_lines WHERE sport='wnba' AND side='over' "
                     "AND stat=? AND DATE(collected_at)=? AND LOWER(player)=? AND odds IS NOT NULL "
                     "ORDER BY collected_at", (stat, date, player.lower())).fetchall()
    first = {}
    for ln, od, ca in rows:
        if ln not in first:
            first[ln] = od
    gap = GAP.get(stat, 1.0)
    rungs, last = [], None
    for ln in sorted(x for x in first if x >= base):
        if last is None or ln - last >= gap:
            rungs.append((ln, first[ln])); last = ln
    return rungs[:4]

lad = []
for (date, player, stat), r in plays.items():
    rungs = real_ladder(date, player, stat, r["line"])
    if not rungs:
        continue
    actual = r["actual"]
    lad.append({"date": date, "player": player, "stat": stat, "band": r.get("d_min"),
                "rungs": [(ln, od, actual > ln) for ln, od in rungs], "base_win": r["result"] == "over"})

def construct(stakes):
    tot_stake = tot_ret = 0.0
    for p in lad:
        for i, (ln, od, win) in enumerate(p["rungs"]):
            if i >= len(stakes) or stakes[i] == 0:
                continue
            tot_stake += stakes[i]
            tot_ret += stakes[i] * od if win else 0
    u = tot_ret - tot_stake
    return u, tot_stake, (u / tot_stake * 100 if tot_stake else 0)

print(f"selected graded plays with real ladders: {len(lad)}")
print("\n── rung hit rate by depth (real alt lines) ──")
for i, name in enumerate(("base", "rung 2", "rung 3", "rung 4")):
    rs = [p["rungs"][i] for p in lad if len(p["rungs"]) > i]
    if rs:
        w = sum(1 for _, _, win in rs if win)
        am = sum(od for _, od, _ in rs) / len(rs)
        print(f"  {name:<7} {w}/{len(rs)} = {w/len(rs)*100:3.0f}% hit · avg odds {am:.2f}")

print("\n── stake constructions (ROI per unit risked) ──")
for name, st in (("base only 1u", [1]), ("current 1/.5/.25", [1, .5, .25]),
                 ("flat 1/1/1", [1, 1, 1]), ("deep 1/.5/.25/.25", [1, .5, .25, .25]),
                 ("top-heavy 1/.75/.5", [1, .75, .5]), ("two-rung 1/.5", [1, .5])):
    u, stk, roi = construct(st)
    print(f"  {name:<18} {u:+7.2f}u on {stk:5.1f}u staked = {roi:+5.1f}% ROI")

print("\n── band-scaled: deep ladders ONLY on d_min 3-8 plays, base-only otherwise ──")
u = stk = 0.0
for p in lad:
    inband = p["band"] is not None and 3 <= p["band"] <= 8
    stakes = [1, .5, .25] if inband else [1]
    for i, (ln, od, win) in enumerate(p["rungs"]):
        if i >= len(stakes):
            continue
        stk += stakes[i]; u += stakes[i] * od - stakes[i] if win else -stakes[i]
print(f"  band-scaled          {u:+7.2f}u on {stk:5.1f}u staked = {u/stk*100:+5.1f}% ROI")

# ═══ PARLAYS from the same selected base legs ═══
print("\n── parlays: every rule-legal combo of the day's selected legs (real odds product) ──")
bydate = defaultdict(list)
for p in lad:
    r = plays[(p["date"], p["player"], p["stat"])]
    bydate[p["date"]].append({"player": p["player"], "team": r.get("team"), "stat": p["stat"],
                              "odds": float(r["odds"]), "win": p["base_win"]})
def legal(combo):
    if len({l["player"] for l in combo}) != len(combo):
        return False
    teams = [l["team"] for l in combo if l["team"]]
    if any(teams.count(t) > 2 for t in set(teams)):
        return False
    for a, b in combinations(combo, 2):
        if a["team"] and a["team"] == b["team"]:
            ca = S.COMPONENTS.get(a["stat"], set()); cb = S.COMPONENTS.get(b["stat"], set())
            if ca & cb:
                return False
    return True

res = defaultdict(lambda: [0, 0, 0.0])
same_pair = [0, 0]; cross_pair = [0, 0]
for date, legs in bydate.items():
    for n in (2, 3):
        for combo in combinations(legs, n):
            if not legal(combo):
                continue
            win = all(l["win"] for l in combo)
            dec = 1.0
            for l in combo:
                dec *= l["odds"]
            res[f"{n}-leg all"][0] += win; res[f"{n}-leg all"][1] += 1
            res[f"{n}-leg all"][2] += (dec - 1) if win else -1.0
            if n == 2:
                st = combo[0]["team"] and combo[0]["team"] == combo[1]["team"]
                (same_pair if st else cross_pair)[0] += win
                (same_pair if st else cross_pair)[1] += 1
    # policy: top-3 by shortest odds vs current top-3 by EV-ish (odds proxy: use dec asc = favorite)
    legs_sorted = sorted(legs, key=lambda l: l["odds"])
    for name, pool in (("2-leg favorites", legs_sorted[:3]),):
        for combo in combinations(pool, 2):
            if not legal(combo):
                continue
            win = all(l["win"] for l in combo)
            dec = combo[0]["odds"] * combo[1]["odds"]
            res[name][0] += win; res[name][1] += 1
            res[name][2] += (dec - 1) if win else -1.0
for k in sorted(res):
    w, n, u = res[k]
    if n:
        print(f"  {k:<16} {w}/{n} hit ({w/n*100:3.0f}%) · {u:+6.1f}u flat-1u = {u/n*100:+4.0f}% ROI")
print(f"  same-team pairs  {same_pair[0]}/{same_pair[1]} joint-hit" if same_pair[1] else "  (no same-team pairs)")
print(f"  cross-team pairs {cross_pair[0]}/{cross_pair[1]} joint-hit")
