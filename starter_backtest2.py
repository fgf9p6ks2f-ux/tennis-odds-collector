"""CORRECTED starter-replacement accuracy. The first pass miscounted games whose lineup didn't map
cleanly as 'absorbed'. This only uses games where BOTH teams' fives are fully captured (len==5) and
finds genuine 1-for-1 swaps between consecutive games: a prior starter who didn't play, replaced by
exactly one new starter. That new starter IS the answer — so we can grade the depth engine's
prediction directly. Leak-free (predict off games strictly before).

    python starter_backtest2.py [days]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import wnba_backtest_layers as B
import wnba_depth as D
import wnba_wowy as W

CACHE = Path(__file__).resolve().parent / "wnba_starter_cache.json"


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    players = W.players()
    n2team = {n: str(v.get("team", "")) for n, v in players.items()}
    n2id = {n: v["id"] for n, v in players.items()}
    n2pos = {n: v.get("position") for n, v in players.items()}
    logc = {}

    def plog(pid):
        if pid not in logc:
            try:
                logc[pid] = sorted((g for g in W.game_log(pid) if g["min"] > 0), key=lambda g: g["date"])
            except Exception:
                logc[pid] = []
        return logc[pid]

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    tg = defaultdict(list)
    for gid, date in B.game_ids(days):
        s = cache.get(gid)
        if not s:
            continue
        stb, plb = defaultdict(set), defaultdict(set)
        for nm, isst in s.items():
            t = n2team.get(nm)
            if not t:
                continue
            plb[t].add(nm)
            if isst:
                stb[t].add(nm)
        for t in plb:
            if len(stb[t]) == 5:                         # CLEAN: team's five fully captured
                tg[t].append((date, plb[t], stb[t]))

    events = h1 = h3 = 0
    misses = []
    for team, gl in tg.items():
        gl.sort(key=lambda x: x[0])
        for i in range(1, len(gl)):
            date, curr_played, curr_st = gl[i]
            prev_st = gl[i - 1][2]
            out = [x for x in prev_st if x not in curr_played]     # prior starter who didn't play
            new = [x for x in curr_st if x not in prev_st]         # the new starter(s)
            if len(out) != 1 or len(new) != 1:                     # clean 1-for-1 swap only
                continue
            ox, actual = out[0], new[0]
            opid = n2id.get(ox)
            olog = [g for g in plog(opid) if g["date"][:10] < date] if opid else []
            if len(olog) < 4:
                continue
            rot = []
            for nm in {n for n, t in n2team.items() if t == team}:
                pid = n2id.get(nm)
                pre = [g for g in plog(pid) if g["date"][:10] < date] if pid else []
                if len(pre) >= 4:
                    rot.append((pid, nm, n2pos.get(nm), pre))
            preds = D.replacements(opid, n2pos.get(ox), olog, rot)
            if not preds:
                continue
            events += 1
            names = [p["name"] for p in preds]
            if names and names[0] == actual:
                h1 += 1
            if actual in names[:3]:
                h3 += 1
            else:
                misses.append((team, ox, names[0] if names else "-", actual))

    print(f"\nSTARTER-REPLACEMENT ACCURACY (corrected) — {events} clean 1-for-1 swaps, last {days} days\n")
    if events:
        print(f"  our TOP-1 pick was the actual replacement:  {h1}/{events} ({100*h1/events:.0f}%)")
        print(f"  actual replacement in our TOP-3:            {h3}/{events} ({100*h3/events:.0f}%)")
        print(f"\n  misses (out -> our#1 vs ACTUAL replacement):")
        for t, ox, p1, act in misses[:8]:
            print(f"    {t}: {ox} out -> we said {p1}, actually {act}")


if __name__ == "__main__":
    main()
