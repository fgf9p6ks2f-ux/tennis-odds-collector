"""How accurate is the depth-chart REPLACEMENT engine at predicting who actually STARTS when a
starter is out? Grades depth.replacements() against ESPN's real starter flag (game_starters).

For each team-game: take the usual starting five (modal over the prior 5 games), find a usual
starter who did NOT play (out), and the player(s) newly promoted into the five. Run the engine on
games strictly before this one and check whether the actual promoted starter is our top-1 / top-3
prediction. Leak-free.

    python starter_backtest.py [days]
"""
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

import wnba_backtest_layers as B
import wnba_depth as D
import wnba_tonight as T
import wnba_wowy as W

CACHE = Path(__file__).resolve().parent / "wnba_starter_cache.json"


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    players = W.players()
    n2team = {n: str(v.get("team", "")) for n, v in players.items()}
    n2id = {n: v["id"] for n, v in players.items()}
    n2pos = {n: v.get("position") for n, v in players.items()}
    logcache = {}

    def plog(pid):
        if pid not in logcache:
            try:
                logcache[pid] = sorted((g for g in W.game_log(pid) if g["min"] > 0),
                                       key=lambda g: g["date"])
            except Exception:
                logcache[pid] = []
        return logcache[pid]

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    dirty = False
    team_games = defaultdict(list)                       # team -> [(date, played_set, starters_set)]
    for gid, date in B.game_ids(days + 20):
        if gid not in cache:
            s = T.game_starters(gid)
            cache[gid] = s or {}
            dirty = True
        s = cache[gid]
        if not s:
            continue
        by_team_start, by_team_played = defaultdict(set), defaultdict(set)
        for name, isst in s.items():
            t = n2team.get(name)
            if not t:
                continue
            by_team_played[t].add(name)
            if isst:
                by_team_start[t].add(name)
        for t in by_team_played:
            team_games[t].append((date, by_team_played[t], by_team_start[t]))
    if dirty:
        CACHE.write_text(json.dumps(cache))

    cutoff_all = sorted({d for gl in team_games.values() for d, *_ in gl})
    cutoff = cutoff_all[-min(days, len(cutoff_all))] if cutoff_all else "9999"
    events = h1 = h3 = named = 0
    misses = []
    for team, gl in team_games.items():
        gl.sort(key=lambda x: x[0])
        for i, (date, played, starters) in enumerate(gl):
            if date < cutoff or i < 5:
                continue
            prior = gl[:i]
            usual = {n for n, _ in Counter(s for _, _, ss in prior[-5:] for s in ss).most_common(5)}
            out = [x for x in usual if x not in played]      # a usual starter who didn't play = OUT
            promoted = [x for x in starters if x not in usual]   # newly in the five
            if not out or not promoted:
                continue
            # rotation from games strictly before this one
            rot = []
            teammates = {n for n, t in n2team.items() if t == team}
            for nm in teammates:
                pid = n2id.get(nm)
                pre = [g for g in plog(pid) if g["date"][:10] < date] if pid else []
                if len(pre) >= 4:
                    rot.append((pid, nm, n2pos.get(nm), pre))
            for ox in out:
                opid = n2id.get(ox)
                olog = [g for g in plog(opid) if g["date"][:10] < date] if opid else []
                if not olog:
                    continue
                preds = D.replacements(opid, n2pos.get(ox), olog, rot)
                if not preds:
                    continue
                events += 1
                names = [p["name"] for p in preds]
                if names and names[0] in promoted:
                    h1 += 1
                if any(nm in promoted for nm in names[:3]):
                    h3 += 1
                else:
                    misses.append((team, ox, names[0] if names else "-", list(promoted)[:2]))

    print(f"\nSTARTER-PREDICTION ACCURACY — last {days} days, {events} out-starter events\n")
    if events:
        print(f"  our TOP-1 pick actually started:   {h1}/{events} ({100*h1/events:.0f}%)")
        print(f"  actual replacement in our TOP-3:   {h3}/{events} ({100*h3/events:.0f}%)")
        print(f"\n  a few misses (team · out → our#1 vs who ACTUALLY started):")
        for t, ox, p1, actual in misses[:6]:
            print(f"    {t}: {ox} out → we said {p1}, actually {', '.join(actual)}")


if __name__ == "__main__":
    main()
