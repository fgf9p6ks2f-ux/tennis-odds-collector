"""EMPIRICAL replacement model — learns, per (team, vacated-position), WHO a team actually promotes
into the starting five, from clean ESPN-starter-flag swaps.

Correction (2026-07-11): an earlier version mis-counted games whose lineup didn't map cleanly as
'role absorbed' and concluded 86% absorption. That was a bug — someone ALWAYS fills the fifth spot.
So this learns from CLEAN 1-for-1 swaps only (a prior starter didn't play, exactly one new starter
appeared) and offers the team's historical replacement-by-position to reorder the depth engine's
shortlist. Sample is tiny early (the depth engine's top-3 ~60% is the workhorse); this sharpens as
the season logs starter flags. The BET keys on usage-WOWY, not this (starter top-1 is only ~0-20%).

    python wnba_lineup_model.py [days]     # refresh starter cache, train, ship, backtest
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import wnba_backtest_layers as B
import wnba_depth as D
import wnba_tonight as T
import wnba_wowy as W

CACHE = Path(__file__).resolve().parent / "wnba_starter_cache.json"
MODEL = Path(__file__).resolve().parent / "wnba_lineup_model.json"
MIN_SEEN = 2                 # trust a team's pattern only after it's repeated it >=2x


def load_model():
    if not MODEL.exists():
        return {}
    return {tuple(k.split("|", 1)): Counter(v) for k, v in json.loads(MODEL.read_text()).items()}


def likely_starter(team, pos, model=None):
    """The team's historically-most-common replacement for a starter at `pos` — or None until the
    team has actually repeated the pattern (MIN_SEEN). Used only to reorder the depth shortlist."""
    model = model if model is not None else load_model()
    c = model.get((str(team), str(pos)))
    if not c:
        return None
    name, cnt = c.most_common(1)[0]
    return name if cnt >= MIN_SEEN else None


def _load(days_pad=70):
    players = W.players()
    n2team = {n: str(v.get("team", "")) for n, v in players.items()}
    n2pos = {n: D._pos(v.get("position")) for n, v in players.items()}
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    dirty = False
    tg = defaultdict(list)
    for gid, date in B.game_ids(days_pad):
        if gid not in cache:
            cache[gid] = T.game_starters(gid) or {}
            dirty = True
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
            if len(stb[t]) == 5:                         # CLEAN: skip games whose five didn't map
                tg[t].append((date, plb[t], stb[t]))
    if dirty:
        CACHE.write_text(json.dumps(cache))
    return tg, n2pos


def _swaps(tg, n2pos, upto=None):
    """clean 1-for-1 swaps -> (team, out_pos, replacement_name), strictly before `upto` if given."""
    for team, gl in tg.items():
        gl = sorted(gl, key=lambda x: x[0])
        for i in range(1, len(gl)):
            date, played, st = gl[i]
            if upto and date >= upto:
                continue
            prev = gl[i - 1][2]
            out = [x for x in prev if x not in played]
            new = [x for x in st if x not in prev]
            if len(out) == 1 and len(new) == 1:
                yield team, n2pos.get(out[0], "F"), new[0]


def train(tg, n2pos, upto=None):
    m = defaultdict(Counter)
    for team, pos, repl in _swaps(tg, n2pos, upto):
        m[(team, pos)][repl] += 1
    return m


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    tg, n2pos = _load()
    m = train(tg, n2pos)
    MODEL.write_text(json.dumps({f"{t}|{p}": dict(c) for (t, p), c in m.items()}))

    dates = sorted({d for gl in tg.values() for d, *_ in gl})
    cutoff = dates[-min(days, len(dates))] if dates else "9999"
    n = emp1 = emp3 = 0
    for team, gl in tg.items():
        gl = sorted(gl, key=lambda x: x[0])
        for i in range(1, len(gl)):
            date, played, st = gl[i]
            if date < cutoff:
                continue
            prev = gl[i - 1][2]
            out = [x for x in prev if x not in played]
            new = [x for x in st if x not in prev]
            if len(out) != 1 or len(new) != 1:
                continue
            opos = n2pos.get(out[0], "F")
            prior = train(tg, n2pos, upto=date)          # leak-free
            c = prior.get((team, opos), Counter())
            ranked = [nm for nm, _ in c.most_common()]
            n += 1
            emp1 += 1 if ranked and ranked[0] == new[0] else 0
            emp3 += 1 if new[0] in ranked[:3] else 0
    stored = sum(1 for c in m.values() for k, v in c.items() if v >= MIN_SEEN)
    print(f"\nEMPIRICAL replacement model (clean swaps) — {sum(len(c.values()) for c in m.values())} "
          f"training swaps, {stored} repeated (team,pos)->player patterns learned\n")
    print(f"  leak-free backtest on {n} held-out swaps: empirical top-1 "
          f"{100*emp1/n if n else 0:.0f}%, top-3 {100*emp3/n if n else 0:.0f}%")
    print(f"  (depth-engine heuristic was 0% / 62%; empirical only helps once teams repeat "
          f"patterns — accrues forward)")


if __name__ == "__main__":
    main()
