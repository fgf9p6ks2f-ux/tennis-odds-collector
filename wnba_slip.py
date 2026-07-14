#!/usr/bin/env python3
"""Bet-slip constructor: turn the day's flagged OVERS into the user's actual bet structure —
per-player ladders (+ the same-player combo logic) and 2-3 leg parlays from the most confident
ladders, honoring his correlation rules. The model is overs-only upstream, so every leg is an over.

RULES (2026-07-13, user):
- Ladders are the money: a confident over is bet across its rungs (alt lines).
- Same player, multiple overs: ladder the strongest single stat + fold a secondary "kinda-like"
  stat into the COMBO rather than a standalone (really like rebounds + kinda like points -> ladder
  rebounds + play P+R; love both -> points, rebounds, P+R).
- Parlays: 2-3 legs from two confident ladders. Correlation rules:
    * max 2 different players per team;
    * same-team legs must have DISJOINT production components (one rule covering BOTH "no repeat
      prop" — two points legs share P — AND "no reverse correlation" — a rebounds leg + a
      teammate's pts_reb share R, or two bigs' rebounds);
    * cross-team legs are unconstrained (his call).
"""
from itertools import combinations

# the finite production pools each market draws from
COMPONENTS = {"points": frozenset("P"), "rebounds": frozenset("R"), "assists": frozenset("A"),
              "pts_reb": frozenset("PR"), "pts_ast": frozenset("PA"), "reb_ast": frozenset("RA"),
              "pra": frozenset("PRA"), "threes": frozenset("3")}
STAT_LABEL = {"points": "pts", "rebounds": "reb", "assists": "ast", "pts_reb": "P+R",
              "pts_ast": "P+A", "reb_ast": "R+A", "pra": "P+R+A", "threes": "3PM"}


def _comps(stat):
    return COMPONENTS.get(stat, frozenset())


def _am(dec):
    if not dec:
        return "?"
    return f"+{round((dec - 1) * 100)}" if dec >= 2 else f"-{round(100 / (dec - 1))}"


def _dec(o):
    return o.get("dec") or o.get("odds")


def ladders(overs):
    """Group flagged OVERS into per-(player, stat) ladders: rungs sorted low->high, with anchor EV."""
    by = {}
    for o in overs:
        if (o.get("side") or "over") != "over":
            continue
        by.setdefault((o["player"], o["stat"]), []).append(o)
    out = []
    for (player, stat), rungs in by.items():
        rungs = sorted(rungs, key=lambda r: r["line"])
        out.append({"player": player, "team": rungs[0].get("team"), "stat": stat, "rungs": rungs,
                    "ev": max((r.get("ev") or 0) for r in rungs), "comps": _comps(stat)})
    return out


def player_bets(lads):
    """Same-player combo logic. Per player: keep the highest-EV anchor ladder; keep a combo that
    EXTENDS it (shares a component) to carry the secondary; DROP a standalone single that a kept
    combo already contains and out-EVs (it's folded into the combo). Returns {player: [ladders]}."""
    byp = {}
    for L in lads:
        byp.setdefault(L["player"], []).append(L)
    result = {}
    for player, pl in byp.items():
        pl = sorted(pl, key=lambda L: -L["ev"])
        kept = [pl[0]]                                    # anchor = highest EV
        for L in pl[1:]:
            folded = any(len(L["comps"]) == 1 and L["comps"] < k["comps"] and k["ev"] >= L["ev"]
                         for k in kept)                   # single already inside a stronger kept combo
            if not folded:
                kept.append(L)
        result[player] = kept
    return result


def _compatible(a, b):
    """Two parlay legs: OK unless SAME team AND overlapping production components."""
    if a["team"] and a["team"] == b["team"]:
        return _comps(a["stat"]).isdisjoint(_comps(b["stat"]))
    return True


def parlays(lads, sizes=(2, 3), top=3):
    """Best 2-3 leg parlays from the confident ladders (one safest rung per ladder, distinct
    players), honoring max-2-per-team + same-team disjoint components. Parlay EV estimated as
    prod(1+ev_leg)-1 (assumes ~independence; same-team legs are disjoint-pool by rule)."""
    legs = []
    for L in sorted(lads, key=lambda L: -L["ev"]):
        r = L["rungs"][0]                                 # safest (lowest) rung = the parlay leg
        legs.append({"player": L["player"], "team": L["team"], "stat": L["stat"],
                     "line": r["line"], "dec": _dec(r), "ev": L["ev"]})
    seen, out = set(), []
    for n in sizes:
        for combo in combinations(legs, n):
            if len({l["player"] for l in combo}) != n:    # distinct players
                continue
            teams = [l["team"] for l in combo if l["team"]]
            if any(teams.count(t) > 2 for t in teams):    # max 2 players/team
                continue
            if not all(_compatible(a, b) for a, b in combinations(combo, 2)):
                continue
            key = frozenset((l["player"], l["stat"], l["line"]) for l in combo)
            if key in seen:
                continue
            seen.add(key)
            dec = 1.0
            ev = 1.0
            for l in combo:
                dec *= (l["dec"] or 1)
                ev *= (1 + l["ev"])
            out.append({"legs": list(combo), "dec": round(dec, 2), "ev": ev - 1, "n": n})
    out.sort(key=lambda p: -p["ev"])
    return out[:top]


def build(overs):
    """Full slip from the day's flagged overs: {'bets': {player: [ladders]}, 'parlays': [...]}."""
    lads = ladders(overs)
    bets = player_bets(lads)
    kept = [L for pl in bets.values() for L in pl]        # ladders that survived the combo logic
    return {"bets": bets, "parlays": parlays(kept)}


def render(slip):
    """Plain-text slip for daily.md / notifications."""
    lines = ["STRAIGHTS & LADDERS (overs):"]
    for player in sorted(slip["bets"], key=lambda p: -max(L["ev"] for L in slip["bets"][p])):
        for L in sorted(slip["bets"][player], key=lambda L: -L["ev"]):
            rungs = " / ".join(f"o{r['line']:g} {_am(_dec(r))}" for r in L["rungs"])
            tag = "  ⟵ LADDER" if len(L["rungs"]) > 1 else ""
            lines.append(f"  {player} {STAT_LABEL.get(L['stat'], L['stat'])}: {rungs}"
                         f"  (+{L['ev'] * 100:.0f}%){tag}")
    if slip["parlays"]:
        lines.append("\nPARLAYS (from confident ladders):")
        for p in slip["parlays"]:
            legs = "  ×  ".join(f"{l['player'].split()[-1]} {STAT_LABEL.get(l['stat'], l['stat'])} "
                                f"o{l['line']:g}" for l in p["legs"])
            lines.append(f"  {p['n']}-leg @ {_am(p['dec'])} (+{p['ev'] * 100:.0f}%):  {legs}")
    return "\n".join(lines)


if __name__ == "__main__":
    # demo on a synthetic slate
    demo = [
        {"player": "A Center", "team": "X", "stat": "rebounds", "line": 7.5, "dec": 1.9, "ev": 0.15, "side": "over"},
        {"player": "A Center", "team": "X", "stat": "rebounds", "line": 9.5, "dec": 2.4, "ev": 0.11, "side": "over"},
        {"player": "A Center", "team": "X", "stat": "pts_reb", "line": 18.5, "dec": 1.9, "ev": 0.12, "side": "over"},
        {"player": "A Center", "team": "X", "stat": "points", "line": 10.5, "dec": 1.85, "ev": 0.08, "side": "over"},
        {"player": "B Guard", "team": "X", "stat": "assists", "line": 5.5, "dec": 1.95, "ev": 0.13, "side": "over"},
        {"player": "C Wing", "team": "Y", "stat": "points", "line": 14.5, "dec": 1.9, "ev": 0.14, "side": "over"},
    ]
    print(render(build(demo)))
