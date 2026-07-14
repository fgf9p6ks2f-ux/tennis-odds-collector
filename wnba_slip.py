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
import json
import sqlite3
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"        # parlays live in the same DB as the straights (self-heals)

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


PARLAY_EPOCH = "2026-07-13"                  # overs-only + slip era; parlay record starts here

_SCHEMA = """CREATE TABLE IF NOT EXISTS parlays(
  pred_date TEXT, key TEXT, legs TEXT, n INTEGER, dec REAL, ev REAL,
  result TEXT, pnl REAL, graded INTEGER DEFAULT 0, graded_at TEXT,
  UNIQUE(pred_date, key));"""


def _pcon():
    con = sqlite3.connect(LEDGER)
    con.execute(_SCHEMA)
    return con


def _key(legs):
    return "|".join(sorted(f"{l['player']}/{l['stat']}/{l['line']:g}" for l in legs))


def log_parlays(date, pars):
    """Persist the day's recommended parlays for grading. Each scan REPLACES the still-pending set
    for the date (parlays are live suggestions built from the current ladders, not locked like a
    placed straight) — graded parlays are never touched. Flat 1u stake per parlay."""
    con = _pcon()
    con.execute("DELETE FROM parlays WHERE pred_date=? AND graded=0", (date,))
    for p in pars:
        legs = [{"player": l["player"], "team": l.get("team"), "stat": l["stat"],
                 "line": l["line"], "side": "over", "odds": l.get("dec")} for l in p["legs"]]
        con.execute("INSERT OR IGNORE INTO parlays(pred_date,key,legs,n,dec,ev,result) "
                    "VALUES(?,?,?,?,?,?,'pending')",
                    (date, _key(legs), json.dumps(legs), p["n"], p["dec"], p["ev"]))
    con.commit()
    con.close()


def grade_parlays():
    """Grade pending parlays whose legs have ALL settled, against the graded predictions. A voided
    leg drops out and the parlay reprices on the survivors: loses if any non-void leg lost, wins if
    all non-void legs won (payout = product of won legs' odds), void if every leg voided."""
    con = _pcon()
    con.row_factory = sqlite3.Row
    pend = con.execute("SELECT rowid, pred_date, legs FROM parlays WHERE graded=0").fetchall()
    n = 0
    for row in pend:
        legs = json.loads(row["legs"])
        st = []
        for l in legs:
            r = con.execute(
                "SELECT result FROM predictions WHERE pred_date=? AND player=? AND stat=? "
                "AND ABS(line-?)<1e-6 AND graded=1",
                (row["pred_date"], l["player"], l["stat"], l["line"])).fetchone()
            if not r:
                st.append("pending")
            elif r["result"] in (None, "push", "void"):
                st.append("void")
            elif r["result"] == (l.get("side") or "over"):
                st.append(("win", l))
            elif r["result"] in ("over", "under"):
                st.append("loss")
            else:
                st.append("pending")
        if any(s == "pending" for s in st):
            continue                                     # not all legs settled -> leave pending
        if any(s == "loss" for s in st):
            result, pnl = "loss", -1.0
        else:
            won = [s[1] for s in st if isinstance(s, tuple)]
            if not won:
                result, pnl = "void", 0.0
            else:
                payout = 1.0
                for l in won:
                    payout *= (l["odds"] or 1)
                result, pnl = "win", payout - 1.0
        con.execute("UPDATE parlays SET result=?, pnl=?, graded=1, graded_at=datetime('now') "
                    "WHERE rowid=?", (result, pnl, row["rowid"]))
        n += 1
    con.commit()
    con.close()
    return n


def sync_parlays():
    """Self-heal + grade, run every grade cycle by BOTH loops (like odds_other): for each slate that
    hasn't started settling yet, REBUILD its parlays from the predictions-table overs and persist them
    — so a stale-loop ledger commit can't leave the parlays table wiped. A slate is frozen the moment
    any leg grades (rebuilding from only-unsettled legs then would mangle a placed parlay). Returns the
    number of parlays graded."""
    con = _pcon()
    con.row_factory = sqlite3.Row
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT pred_date FROM predictions WHERE result IS NULL "
        "AND (side='over' OR side IS NULL)").fetchall()]
    fresh = {}
    for d in dates:
        if con.execute("SELECT 1 FROM predictions WHERE pred_date=? AND graded=1 LIMIT 1", (d,)).fetchone():
            continue                                      # slate settling -> freeze its parlays
        fresh[d] = [dict(r) for r in con.execute(
            "SELECT player, team, stat, line, side, odds, ev FROM predictions WHERE pred_date=? "
            "AND result IS NULL AND (side='over' OR side IS NULL)", (d,)).fetchall()]
    con.close()
    for d, overs in fresh.items():
        if overs:
            log_parlays(d, build(overs)["parlays"])
    return grade_parlays()


def parlay_record(epoch=PARLAY_EPOCH):
    """(w, l, void, units, roi, pending) over graded parlays since epoch, flat 1u. ROI over the
    W+L settled (voids excluded from the denominator)."""
    con = _pcon()
    con.row_factory = sqlite3.Row
    g = con.execute("SELECT result, pnl FROM parlays WHERE graded=1 AND pred_date>=?",
                    (epoch,)).fetchall()
    pending = con.execute("SELECT COUNT(*) FROM parlays WHERE graded=0 AND pred_date>=?",
                          (epoch,)).fetchone()[0]
    con.close()
    w = sum(1 for r in g if r["result"] == "win")
    l = sum(1 for r in g if r["result"] == "loss")
    void = sum(1 for r in g if r["result"] == "void")
    units = sum((r["pnl"] or 0) for r in g)
    roi = units / (w + l) if (w + l) else 0.0
    return w, l, void, units, roi, pending


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
