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
import hashlib
import json
import sqlite3
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"        # parlays live in the same DB as the straights (self-heals)
PARLAY_MARKS = HERE / "wnba_parlays_played.txt"   # durable played marks (date|key), like wnba_played.txt


def _stake(dec):
    """User's parlay staking (2026-07-13): 0.25u, but 0.15u on a longshot at +1000 (dec 11.0) or longer."""
    return 0.15 if (dec or 0) >= 11.0 else 0.25


# STRAIGHT-LADDER staking (2026-07-14, user): 1u on the base (main -110) line, then DECLINING rungs up
# the ladder (0.5 / 0.25 / 0.25 ...), total per player-stat ladder CAPPED at 2.5u — the exposure cap so
# one cold game fully laddered (e.g. Copper's 3 rungs on a 9-pt night) can't run past 2.5u.
LADDER_ANCHOR_U = 1.0
LADDER_RUNG_US = (0.5, 0.25, 0.25)
LADDER_RUNG_DEFAULT_U = 0.25
LADDER_CAP_U = 2.5
# per-player-GAME cap: at most COMPONENT_CAP_U riding on any ONE production component (P/R/A) in a
# single player-game. Scales down CORRELATED stacking (Copper: points+pts_reb+pra all key on points ->
# 3u -> 2.5u) while leaving DISJOINT bets alone (Stewart: points+assists+rebounds are uncorrelated ->
# untouched). Same disjoint-pool logic as the parlay rule. Validated: worst 1-game loss -3.0u -> -2.5u.
COMPONENT_CAP_U = 2.5


def ladder_stake_map(rows):
    """Map each OVER row to its stake: {(pred_date, player, stat, line): stake}. TWO caps: (1) per
    player-STAT ladder — lowest line is the 1u anchor, higher rungs decline (0.5/0.25/0.25...), total
    capped at LADDER_CAP_U; (2) per player-GAME — exposure to any one component (P/R/A) capped at
    COMPONENT_CAP_U, scaling correlated stacking down (a lone over is just the 1u anchor)."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        if (r.get("side") or "over") != "over":
            continue
        groups[(r.get("pred_date"), r.get("player"), r.get("stat"))].append(r)
    out = {}
    for rungs in groups.values():
        total = 0.0
        for i, r in enumerate(sorted(rungs, key=lambda x: (x.get("line") or 0))):
            base = LADDER_ANCHOR_U if i == 0 else (
                LADDER_RUNG_US[i - 1] if i - 1 < len(LADDER_RUNG_US) else LADDER_RUNG_DEFAULT_U)
            s = min(base, max(0.0, LADDER_CAP_U - total))
            total += s
            out[(r.get("pred_date"), r.get("player"), r.get("stat"), r.get("line"))] = s
    # (2) per-player-game component cap
    comp_exp = defaultdict(lambda: defaultdict(float))     # (date,player) -> component -> exposure
    for (d, p, stat, ln), stake in out.items():
        for c in _comps(stat):
            comp_exp[(d, p)][c] += stake
    for k in list(out):
        exps = comp_exp[(k[0], k[1])]
        mx = max(exps.values()) if exps else 0.0
        if mx > COMPONENT_CAP_U:
            out[k] *= COMPONENT_CAP_U / mx
    return out


def _pid(date, key):
    """Short stable id for a parlay (shown on the dashboard; used to mark it played)."""
    return hashlib.sha1(f"{date}|{key}".encode()).hexdigest()[:4]

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


def current_selection(rows):
    """The subset of OVER rows the CURRENT model would actually pick — used to restate the tracked
    record to the new bot's selection. Drops two rule-based classes (returns (kept, dropped) where
    dropped is [(row, reason)]):
      1. THIN-SAMPLE over-extrapolation — n_elev<7 AND d_min>10 (the shipped guard; the Ayayi 0-5 pattern).
      2. CORRELATED over-stack — per player-game, when 3+ DISTINCT stats share a production pool (P/R/A),
         keep only the highest-EV stat (its ladder rungs included) and drop the redundant ones (Copper
         bet points+pts_reb+pra, all keyed on her scoring). 2 correlated stats (a stat + one extending
         combo, e.g. rebounds + P+R) are the user's intentional construction and are kept."""
    from collections import defaultdict
    overs = [r for r in rows if (r.get("side") or "over") == "over"]
    kept, dropped = [], []

    def thin(r):
        n, dm = r.get("n_elev"), r.get("d_min")
        return n is not None and dm is not None and n < 7 and dm > 10

    survivors = []
    for r in overs:
        (dropped.append((r, "thin-sample guard")) if thin(r) else survivors.append(r))

    bypg = defaultdict(list)
    for r in survivors:
        bypg[(r.get("pred_date"), r.get("player"))].append(r)
    for rr in bypg.values():
        stat_ev = defaultdict(lambda: -1.0)
        for r in rr:
            stat_ev[r["stat"]] = max(stat_ev[r["stat"]], r.get("ev") or 0.0)
        stats = list(stat_ev)
        parent = {s: s for s in stats}                    # union-find: cluster stats that share a pool

        def find(s):
            while parent[s] != s:
                parent[s] = parent[parent[s]]
                s = parent[s]
            return s
        for i, a in enumerate(stats):
            for b in stats[i + 1:]:
                if _comps(a) & _comps(b):
                    parent[find(a)] = find(b)
        clusters = defaultdict(list)
        for s in stats:
            clusters[find(s)].append(s)
        keep_stats = set()
        for cl in clusters.values():
            keep_stats.add(max(cl, key=lambda s: stat_ev[s])) if len(cl) >= 3 else keep_stats.update(cl)
        for r in rr:
            (kept if r["stat"] in keep_stats else dropped).append(
                r if r["stat"] in keep_stats else (r, "correlated over-stack (3+ stats share a pool)"))

    # 3. ONE PER INJURY CASCADE (2026-07-15, user + backtest: 15-6/+27% vs scatter 21-19/+0.7%). An
    # injury vacates usage that CONCENTRATES into a single beneficiary (Edwards ate CON's frontcourt
    # 7/14 -> 29 P+R while Miller/Nelson-Ododa got squeezed), so per (date, team) cascade keep only the
    # FAVORITE (shortest book odds = the market's most-likely beneficiary; tie -> higher EV) and ladder
    # THEM; drop the rest. Favorite beat EV/proj_hit in the backtest and matches the user's real method.
    bycas = defaultdict(list)
    for r in kept:
        bycas[(r.get("pred_date"), r.get("team"))].append(r)
    fav_kept = []
    for grp in bycas.values():
        byp = defaultdict(list)
        for r in grp:
            byp[r.get("player")].append(r)
        fav = min(byp, key=lambda p: (min((x.get("odds") or 99) for x in byp[p]),
                                      -max((x.get("ev") or 0.0) for x in byp[p])))
        for r in grp:
            (fav_kept if r.get("player") == fav else dropped).append(
                r if r.get("player") == fav else (r, "non-favorite beneficiary (1 per cascade)"))
    kept = fav_kept
    return kept, dropped


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
    """Full slip from the day's flagged overs: {'bets': {player: [ladders]}, 'parlays': [...]}.
    Overs are reduced to current_selection FIRST (favorite-only per injury cascade + thin-sample &
    over-stack filters), so every ladder and parlay slip only ever contains the ONE beneficiary per
    injury the tracked record also counts — source-of-truth so sync_parlays/wnba_alert stay consistent."""
    overs = current_selection(overs)[0]
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
  result TEXT, pnl REAL, played INTEGER DEFAULT 0, graded INTEGER DEFAULT 0, graded_at TEXT,
  UNIQUE(pred_date, key));"""


def _pcon():
    con = sqlite3.connect(LEDGER)
    con.execute(_SCHEMA)
    if "played" not in {r[1] for r in con.execute("PRAGMA table_info(parlays)")}:
        con.execute("ALTER TABLE parlays ADD COLUMN played INTEGER DEFAULT 0")
    con.commit()
    return con


def _apply_parlay_marks(con):
    """Re-apply the durable played marks (wnba_parlays_played.txt: date|key) onto the DB, so a
    rebuilt/clobbered parlays table still reflects what the user actually bet."""
    if not PARLAY_MARKS.exists():
        return
    for ln in PARLAY_MARKS.read_text().splitlines():
        p = ln.strip().split("|", 1)
        if len(p) == 2:
            con.execute("UPDATE parlays SET played=1 WHERE pred_date=? AND key=?", (p[0], p[1]))
    con.commit()


def _key(legs):
    return "|".join(sorted(f"{l['player']}/{l['stat']}/{l['line']:g}" for l in legs))


def log_parlays(date, pars):
    """Persist the day's recommended parlays for grading. Each scan REPLACES the still-pending set
    for the date (parlays are live suggestions built from the current ladders, not locked like a
    placed straight) — graded parlays are never touched. Flat 1u stake per parlay."""
    con = _pcon()
    # drop only the still-pending, NOT-yet-played suggestions; a played parlay is a placed bet and
    # survives the rebuild (like a flagged straight) until it grades.
    con.execute("DELETE FROM parlays WHERE pred_date=? AND graded=0 AND played=0", (date,))
    for p in pars:
        legs = [{"player": l["player"], "team": l.get("team"), "stat": l["stat"],
                 "line": l["line"], "side": "over", "odds": l.get("dec")} for l in p["legs"]]
        con.execute("INSERT OR IGNORE INTO parlays(pred_date,key,legs,n,dec,ev,result) "
                    "VALUES(?,?,?,?,?,?,'pending')",
                    (date, _key(legs), json.dumps(legs), p["n"], p["dec"], p["ev"]))
    _apply_parlay_marks(con)
    con.commit()
    con.close()


def grade_parlays():
    """Grade pending parlays whose legs have ALL settled, against the graded predictions. A voided
    leg drops out and the parlay reprices on the survivors: loses if any non-void leg lost, wins if
    all non-void legs won (payout = product of won legs' odds), void if every leg voided."""
    con = _pcon()
    con.row_factory = sqlite3.Row
    _apply_parlay_marks(con)                              # re-assert played state before grading
    pend = con.execute("SELECT rowid, pred_date, legs, dec FROM parlays WHERE graded=0").fetchall()
    n = 0
    for row in pend:
        stake = _stake(row["dec"])                        # .25u, or .15u on a +1000-or-longer parlay
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
            result, pnl = "loss", -stake
        else:
            won = [s[1] for s in st if isinstance(s, tuple)]
            if not won:
                result, pnl = "void", 0.0                 # all legs void -> stake returned
            else:
                payout = 1.0
                for l in won:
                    payout *= (l["odds"] or 1)
                result, pnl = "win", stake * (payout - 1)
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


def parlay_record(epoch=PARLAY_EPOCH, played_only=True):
    """Record of the PLAYED parlays since epoch (the user's actual bets), staked .25u / .15u by odds.
    Returns a dict: w, l, void, units, staked, roi (units/staked), pending (played, unsettled),
    suggested (un-played still-pending suggestions). played_only=False gives the model-suggested record."""
    con = _pcon()
    con.row_factory = sqlite3.Row
    _apply_parlay_marks(con)
    where = "played=1 AND " if played_only else ""
    g = con.execute(f"SELECT result, pnl, dec FROM parlays WHERE graded=1 AND {where}pred_date>=?",
                    (epoch,)).fetchall()
    pending = con.execute(f"SELECT COUNT(*) FROM parlays WHERE graded=0 AND {where}pred_date>=?",
                          (epoch,)).fetchone()[0]
    suggested = con.execute("SELECT COUNT(*) FROM parlays WHERE graded=0 AND played=0 AND pred_date>=?",
                            (epoch,)).fetchone()[0]
    con.close()
    w = sum(1 for r in g if r["result"] == "win")
    l = sum(1 for r in g if r["result"] == "loss")
    void = sum(1 for r in g if r["result"] == "void")
    units = sum((r["pnl"] or 0) for r in g)
    staked = sum(_stake(r["dec"]) for r in g if r["result"] in ("win", "loss"))
    return {"w": w, "l": l, "void": void, "units": units, "staked": staked,
            "roi": (units / staked if staked else 0.0), "pending": pending, "suggested": suggested}


def mark_parlay_played(pid, date=None, unmark=False):
    """Mark a parlay (by its short id + slate date) as PLACED — durable in wnba_parlays_played.txt +
    the DB. date defaults to the latest slate with parlays. Returns the matched leg description or None."""
    con = _pcon()
    con.row_factory = sqlite3.Row
    if date is None:
        r = con.execute("SELECT MAX(pred_date) FROM parlays").fetchone()
        date = r[0] if r else None
    hit = None
    for row in con.execute("SELECT key, legs FROM parlays WHERE pred_date=?", (date,)):
        if _pid(date, row["key"]) == pid:
            hit = (row["key"], row["legs"])
            break
    if not hit:
        con.close()
        return None
    key, legs = hit
    marks = set(PARLAY_MARKS.read_text().splitlines()) if PARLAY_MARKS.exists() else set()
    line = f"{date}|{key}"
    if unmark:
        marks.discard(line)
        con.execute("UPDATE parlays SET played=0 WHERE pred_date=? AND key=?", (date, key))
    else:
        marks.add(line)
        con.execute("UPDATE parlays SET played=1 WHERE pred_date=? AND key=?", (date, key))
    con.commit()
    con.close()
    PARLAY_MARKS.write_text("\n".join(sorted(marks)))
    return " × ".join(f"{l['player'].split()[-1]} {STAT_LABEL.get(l['stat'], l['stat'])} o{l['line']:g}"
                      for l in json.loads(legs))


def list_parlays(date=None):
    """Persisted parlays for a slate (default latest), each with its short id + stake + played mark."""
    con = _pcon()
    con.row_factory = sqlite3.Row
    if date is None:
        r = con.execute("SELECT MAX(pred_date) FROM parlays").fetchone()
        date = r[0] if r else None
    out = []
    for row in con.execute("SELECT key, legs, dec, played, result FROM parlays WHERE pred_date=? "
                           "ORDER BY ev DESC", (date,)):
        legs = " × ".join(f"{l['player'].split()[-1]} {STAT_LABEL.get(l['stat'], l['stat'])} o{l['line']:g}"
                          for l in json.loads(row["legs"]))
        out.append({"pid": _pid(date, row["key"]), "legs": legs, "dec": row["dec"],
                    "stake": _stake(row["dec"]), "played": row["played"], "result": row["result"]})
    con.close()
    return date, out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--played", nargs="+", metavar="PID", help="mark parlay(s) played by short id")
    ap.add_argument("--unplay", nargs="+", metavar="PID", help="un-mark parlay(s) played")
    ap.add_argument("--date", help="slate date (default: latest)")
    ap.add_argument("--grade", action="store_true", help="self-heal + grade parlays")
    ap.add_argument("--list", action="store_true", help="list a slate's parlays with ids")
    ap.add_argument("--report", action="store_true", help="print the played-parlay record")
    a = ap.parse_args()
    if a.played or a.unplay:
        for pid in (a.played or []):
            d = mark_parlay_played(pid, a.date)
            print(f"✓ played #{pid}: {d}" if d else f"no parlay #{pid} on {a.date or 'latest slate'}")
        for pid in (a.unplay or []):
            d = mark_parlay_played(pid, a.date, unmark=True)
            print(f"un-played #{pid}: {d}" if d else f"no parlay #{pid}")
    elif a.grade:
        print(f"graded {sync_parlays()} parlays")
    elif a.list:
        date, ps = list_parlays(a.date)
        print(f"parlays for {date}:")
        for p in ps:
            mk = " ✓PLAYED" if p["played"] else ""
            st = f"{p['result']}" if p["result"] else "pending"
            print(f"  #{p['pid']}  {_am(p['dec'])} · {p['stake']:g}u · {st}{mk}   {p['legs']}")
    elif a.report:
        r = parlay_record()
        print(f"PLAYED parlays: {r['w']}-{r['l']} (void {r['void']}), {r['units']:+.2f}u on "
              f"{r['staked']:.2f}u staked -> ROI {r['roi']:+.0%}; "
              f"{r['pending']} pending, {r['suggested']} un-played suggestions")
    else:
        _demo = [
        {"player": "A Center", "team": "X", "stat": "rebounds", "line": 7.5, "dec": 1.9, "ev": 0.15, "side": "over"},
        {"player": "A Center", "team": "X", "stat": "rebounds", "line": 9.5, "dec": 2.4, "ev": 0.11, "side": "over"},
        {"player": "A Center", "team": "X", "stat": "pts_reb", "line": 18.5, "dec": 1.9, "ev": 0.12, "side": "over"},
        {"player": "A Center", "team": "X", "stat": "points", "line": 10.5, "dec": 1.85, "ev": 0.08, "side": "over"},
        {"player": "B Guard", "team": "X", "stat": "assists", "line": 5.5, "dec": 1.95, "ev": 0.13, "side": "over"},
        {"player": "C Wing", "team": "Y", "stat": "points", "line": 14.5, "dec": 1.9, "ev": 0.14, "side": "over"},
        ]
        print(render(build(_demo)))
