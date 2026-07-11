"""WHY does the replacement engine miss? Categorize every out-starter event from the ESPN starter
flags to find correctable patterns (small-ball, star-absorption, position ambiguity) vs genuine
coach noise. Uses the cache built by starter_backtest.py.

    python starter_analysis.py [days]
"""
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

import wnba_backtest_layers as B
import wnba_depth as D
import wnba_wowy as W

CACHE = Path(__file__).resolve().parent / "wnba_starter_cache.json"


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    players = W.players()
    n2team = {n: str(v.get("team", "")) for n, v in players.items()}
    n2id = {n: v["id"] for n, v in players.items()}
    n2pos = {n: D._pos(v.get("position")) for n, v in players.items()}
    logc = {}

    def amin(nm):                                            # season avg minutes (role size)
        pid = n2id.get(nm)
        if pid not in logc:
            try:
                logc[pid] = [g["min"] for g in W.game_log(pid) if g["min"] > 0]
            except Exception:
                logc[pid] = []
        return st.mean(logc[pid]) if logc[pid] else 0.0

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    team_games = defaultdict(list)
    for gid, date in B.game_ids(days + 20):
        s = cache.get(gid)
        if not s:
            continue
        st_by, pl_by = defaultdict(set), defaultdict(set)
        for nm, isst in s.items():
            t = n2team.get(nm)
            if not t:
                continue
            pl_by[t].add(nm)
            if isst:
                st_by[t].add(nm)
        for t in pl_by:
            team_games[t].append((date, pl_by[t], st_by[t]))

    cats = Counter()
    same_pos = cross_pos = bench_promo = absorbed = 0
    bigout_smallball = bigout_total = 0
    rows = []
    for team, gl in team_games.items():
        gl.sort(key=lambda x: x[0])
        for i, (date, played, starters) in enumerate(gl):
            if i < 5:
                continue
            usual = {n for n, _ in Counter(s for _, _, ss in gl[max(0, i-5):i] for s in ss).most_common(5)}
            for ox in [x for x in usual if x not in played]:      # each OUT usual-starter
                opos = n2pos.get(ox, "F")
                promoted = [x for x in starters if x not in usual]
                if not promoted:
                    absorbed += 1
                    cats["absorbed (no new starter — existing players slid up)"] += 1
                    rows.append((team, ox, opos, "—", "absorbed"))
                    continue
                # the promoted starter closest in position (the one filling ox's slot)
                new = min(promoted, key=lambda p: 0 if n2pos.get(p) == opos else 1)
                npos = n2pos.get(new, "F")
                if npos == opos:
                    same_pos += 1
                    cats["same-position promotion (size-for-size)"] += 1
                else:
                    cross_pos += 1
                    cats[f"CROSS-position ({opos} out -> {npos} started)"] += 1
                if amin(new) < 15:
                    bench_promo += 1
                if opos in ("C", "F"):
                    bigout_total += 1
                    if npos == "G":
                        bigout_smallball += 1
                rows.append((team, ox, opos, f"{new}({npos})", "cross" if npos != opos else "same"))

    tot = same_pos + cross_pos + absorbed
    print(f"\nWHY THE REPLACEMENT ENGINE MISSES — {tot} out-starter events, last {days}+ days\n")
    print("## What actually happens when a starter sits")
    print("```")
    for c, n in cats.most_common():
        print(f"  {n:3}  {c}")
    print("```")
    if tot:
        print(f"same-position (engine's assumption): {same_pos}/{tot} ({100*same_pos/tot:.0f}%)")
        print(f"CROSS-position / small-ball / reshuffle: {cross_pos}/{tot} ({100*cross_pos/tot:.0f}%)")
        print(f"role ABSORBED, no new starter at all: {absorbed}/{tot} ({100*absorbed/tot:.0f}%)")
    if bigout_total:
        print(f"\nBIG (C/F) out -> a GUARD started (small-ball): {bigout_smallball}/{bigout_total} "
              f"({100*bigout_smallball/bigout_total:.0f}%)")
    print(f"\nthe promoted starter was a bench player (<15 min): {bench_promo}")


if __name__ == "__main__":
    main()
