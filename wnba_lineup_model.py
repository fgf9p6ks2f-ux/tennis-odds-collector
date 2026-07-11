"""EMPIRICAL replacement model learned from ESPN starter flags.

The positional heuristic assumes a same-position backup fills the slot — but the data says 86% of
the time the role is ABSORBED (no new starter) and big-outs go small-ball. So instead of guessing
from positions, LEARN each team's actual response: when a starter at position P sits, did they
promote a bench player (and who) or absorb it? Shrunk toward the league base rate so a thin team
sample can't run wild.

Built + graded off wnba_starter_cache.json (populated by starter_backtest.py). This module both
trains the model AND backtests it leak-free vs the old heuristic — if it can't beat the base rate
on this sample, that's reported honestly (it accrues as the season logs more starter flags).

    python wnba_lineup_model.py [days]      # train + leak-free backtest report
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
SHRINK = 4.0                # pseudo-count pulling a team/pos toward the league promotion rate


def load_model():
    """The shipped model for live use in wnba_depth. Returns None if not trained yet."""
    if not MODEL.exists():
        return None
    d = json.loads(MODEL.read_text())
    tp = {tuple(k.split("|", 1)): Counter(v) for k, v in d.get("tp", {}).items()}
    return {"lg_promote": d.get("lg_promote", 0.14), "tp": tp}


def promotes(team, out_pos, model=None):
    """LIVE gate: does this team empirically PROMOTE a bench player for an out at `out_pos` (vs
    absorb it)? -> (bool, promoted_name_or_None). Defaults to False (absorb) — the data says that's
    right 86% of the time — until a team shows a real, sample-backed tendency."""
    model = model or load_model()
    if not model:
        return False, None
    mode, who, _p = predict(model, team, out_pos)
    return mode == "PROMOTE", who


def _events(team_games, n2pos, upto=None):
    """Yield (team, out_pos, outcome) where outcome is 'ABSORBED' or the promoted player's name.
    upto: only events strictly before this date (leak-free)."""
    for team, gl in team_games.items():
        gl = sorted(gl, key=lambda x: x[0])
        for i, (date, played, starters) in enumerate(gl):
            if i < 5 or (upto and date >= upto):
                continue
            usual = {n for n, _ in Counter(s for _, _, ss in gl[max(0, i-5):i] for s in ss).most_common(5)}
            promoted = [x for x in starters if x not in usual]
            for ox in [x for x in usual if x not in played]:
                opos = n2pos.get(ox, "F")
                if not promoted:
                    yield team, opos, "ABSORBED"
                else:
                    new = min(promoted, key=lambda p: 0 if n2pos.get(p) == opos else 1)
                    yield team, opos, new


def train(team_games, n2pos, upto=None):
    """league promotion rate + per (team,pos) outcome distribution."""
    league = Counter()
    tp = defaultdict(Counter)
    for team, opos, outcome in _events(team_games, n2pos, upto):
        league[outcome != "ABSORBED"] += 1
        tp[(team, opos)][outcome] += 1
    lg_promote = league[True] / max(sum(league.values()), 1)
    return {"lg_promote": lg_promote, "tp": tp}


def predict(model, team, out_pos):
    """-> (mode, promoted_name_or_None, promote_prob). Shrinks the team/pos sample toward league."""
    c = model["tp"].get((team, out_pos), Counter())
    n = sum(c.values())
    promotes = sum(v for k, v in c.items() if k != "ABSORBED")
    # credibility-shrunk promotion probability
    p = (promotes + SHRINK * model["lg_promote"]) / (n + SHRINK)
    if p < 0.5:
        return "ABSORBED", None, round(p, 2)
    who = Counter({k: v for k, v in c.items() if k != "ABSORBED"}).most_common(1)
    return "PROMOTE", (who[0][0] if who else None), round(p, 2)


def _load_team_games(days_pad=65):
    players = W.players()
    n2team = {n: str(v.get("team", "")) for n, v in players.items()}
    n2pos = {n: D._pos(v.get("position")) for n, v in players.items()}
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    dirty = False
    tg = defaultdict(list)
    for gid, date in B.game_ids(days_pad):
        if gid not in cache:                              # fetch + cache new finished games' starters
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
            tg[t].append((date, plb[t], stb[t]))
    if dirty:
        CACHE.write_text(json.dumps(cache))
    return tg, n2pos


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    tg, n2pos = _load_team_games()
    dates = sorted({d for gl in tg.values() for d, *_ in gl})
    cutoff = dates[-min(days, len(dates))] if dates else "9999"
    # ship the full-data model for live use (wnba_depth.promotes gates the promotion guess on it)
    m = train(tg, n2pos)
    MODEL.write_text(json.dumps({"lg_promote": round(m["lg_promote"], 3),
                                 "tp": {f"{t}|{p}": dict(c) for (t, p), c in m["tp"].items()}}))

    # leak-free backtest: for each held-out event, train on prior, predict the OUTCOME class
    n = base_hit = emp_hit = emp_promote_named = emp_promote_tot = 0
    test = list(_events(tg, n2pos))                      # all events...
    # re-derive with dates for the held-out split
    for team, gl in tg.items():
        gl = sorted(gl, key=lambda x: x[0])
        for i, (date, played, starters) in enumerate(gl):
            if i < 5 or date < cutoff:
                continue
            usual = {x for x, _ in Counter(s for _, _, ss in gl[max(0, i-5):i] for s in ss).most_common(5)}
            promoted = [x for x in starters if x not in usual]
            for ox in [x for x in usual if x not in played]:
                opos = n2pos.get(ox, "F")
                actual = "ABSORBED" if not promoted else "PROMOTE"
                model = train(tg, n2pos, upto=date)      # only prior games
                mode, who, _p = predict(model, team, opos)
                n += 1
                base_hit += 1 if actual == "ABSORBED" else 0     # "always absorbed" baseline
                emp_hit += 1 if mode == actual else 0
                if mode == "PROMOTE":
                    emp_promote_tot += 1
                    new = min(promoted, key=lambda p: 0 if n2pos.get(p) == opos else 1) if promoted else None
                    emp_promote_named += 1 if who and who == new else 0

    print(f"\nEMPIRICAL LINEUP MODEL — leak-free backtest, last {days} days, {n} out-starter events\n")
    if n:
        print(f"  outcome-class accuracy (absorbed vs promote):")
        print(f"    'always absorbed' baseline: {100*base_hit/n:.0f}%")
        print(f"    empirical model:            {100*emp_hit/n:.0f}%")
        print(f"  when the model calls a PROMOTION, it named the right player: "
              f"{emp_promote_named}/{emp_promote_tot}")
        print(f"\n  verdict: {'beats' if emp_hit > base_hit else 'does NOT beat'} the base rate "
              f"on this sample (Δ {emp_hit-base_hit:+d} events)")


if __name__ == "__main__":
    main()
