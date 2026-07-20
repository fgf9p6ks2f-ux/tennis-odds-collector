"""POINTS-OVER SELECTION AUDIT + haircut shadow (2026-07-19, user).

Question that started this: points overs looked like a money-loser (13-18, -5.31u). Is a
projection HAIRCUT the fix? Audit answer: NO — the loss is almost entirely LEGACY out-of-band
plays the current model already refuses to bet. Split the same 31 graded points overs by what
TODAY's model does with them:

    current model BETS (d_min in [0,8] or cold None):  ~12-9 (57%)  +2.8u   <- profitable
    current model SHADOWS (out-of-band <0 or >8):      ~1-9  (10%)  -8.1u   <- the whole loss

So the 7/18 band gate already plugged the leak. A blanket haircut would just shave the plays
that are already winning. What DOES separate winners from losers inside the bet set is the size
of the projected role jump (elevation over season avg): a MODERATE +3-5 bump lands ~88%, while
both a marginal (<3) and a "model-dreaming" (>=5) bump underperform — but those buckets are n=2-8,
so it's a forward hypothesis, not a gate.

This module changes NO live flag/EV/bet. Re-run at the checkpoint; it reads the live ledger.

    python3 wnba_points_haircut.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"

# the haircut menu was FIT to points plays graded on/before this date -> in-sample/circular. Plays
# graded AFTER are the honest out-of-sample test.
EPOCH = "2026-07-19"

HAIRCUTS = {
    "x0.90": lambda p: p * 0.90,
    "x0.87": lambda p: p * 0.87,
    "x0.84": lambda p: p * 0.84,
    "-1.5":  lambda p: p - 1.5,
    "-2.5":  lambda p: p - 2.5,
}


def points_haircut(proj, level="x0.87"):
    """Candidate live haircut — NOT wired into projections. The audit found the band gate already
    handles the points leak, so this stays parked unless the forward sample says otherwise."""
    return HAIRCUTS.get(level, lambda p: p)(proj)


def _rec(rs):
    w = sum(1 for r in rs if r["result"] == "over")
    n = len(rs)
    u = sum((r["odds"] - 1) if r["result"] == "over" else -1 for r in rs)
    return f"{w}-{n-w} ({w/n*100:.0f}%) {u:+.2f}u" if n else "n=0"


def _overs(where="1=1"):
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT pred_date,player,stat,line,odds,elev_avg,season_avg,d_min,d_fga,proj_min,result,actual "
        "FROM predictions WHERE (side IS NULL OR side='over') AND result IN ('over','under') "
        f"AND ({where})")]
    con.close()
    return rows


def per_prop():
    rows = _overs()
    byp = defaultdict(list)
    for r in rows:
        byp[r["stat"]].append(r)
    print("PER-PROP RECORD (overs, graded)")
    for s in ["points", "rebounds", "assists", "pts_reb", "pts_ast", "reb_ast", "pra"]:
        print(f"  {s:10} {_rec(byp.get(s, []))}")
    print(f"  {'ALL':10} {_rec(rows)}")


def points_regime():
    pts = _overs("stat='points' AND elev_avg IS NOT NULL")
    inband = [r for r in pts if r["d_min"] is not None and 0 <= r["d_min"] <= 8]
    cold = [r for r in pts if r["d_min"] is None]
    oob = [r for r in pts if r["d_min"] is not None and (r["d_min"] < 0 or r["d_min"] > 8)]
    print("POINTS overs by what the CURRENT model does with them")
    print(f"  full historical sample:            {_rec(pts)}")
    print(f"  CURRENT MODEL BETS (in-band+cold): {_rec(inband + cold)}")
    print(f"    in-band d_min [0,8]:             {_rec(inband)}")
    print(f"    cold d_min=None:                 {_rec(cold)}")
    print(f"  SHADOWED, not bet (out-of-band):   {_rec(oob)}   <- the whole loss lives here")


def _player_games(rows):
    """Collapse laddered rungs to distinct (date,player) games — laddered lines on the same game
    are ~perfectly correlated, so counting bets (not games) inflates a signal's apparent strength
    (the '7-1 elevation' was really 4 games, 2 of them one player each). Grade the base rung."""
    from collections import defaultdict
    pg = defaultdict(list)
    for r in rows:
        pg[(r["pred_date"], r["player"])].append(r)
    games = []
    for rs in pg.values():
        r0 = min(rs, key=lambda r: r["line"])
        games.append({**r0, "won": (r0["actual"] or 0) > r0["line"]})
    return games


def _grec(gs):
    w = sum(1 for g in gs if g["won"])
    return f"{w}-{len(gs)-w} ({w/len(gs)*100:.0f}%)" if gs else "n=0"


def _base_min(g):
    # baseline ("with-star") minutes ≈ projected minutes − the WOWY minutes bump. High = consistent
    # starter (minutes already capped, can't grow); low = rotation player with room to expand.
    return (g["proj_min"] or 0) - (g["d_min"] or 0)


def hypotheses():
    # THE validated story (2026-07-20, user): the edge is ROLE EXPANSION, not scoring average. A
    # consistent starter's minutes are capped, so an injury only adds a few shots and the projection
    # overreaches; a rotation player gets BOTH more minutes and more shots. Discriminator = baseline
    # minutes + both-bumps. Tracked at DISTINCT PLAYER-GAME level (dedup ladders). Still small — n<15.
    rows = [r for r in _overs("stat='points' AND elev_avg IS NOT NULL")
            if r["d_min"] is not None and 0 <= r["d_min"] <= 8 and r["season_avg"] is not None]
    games = _player_games(rows)
    print(f"ROLE-EXPANSION HYPOTHESIS [distinct player-games, n={len(games)} — small, forward-tracked]")
    print(f"  in-band overall: {_grec(games)}")
    starter = [g for g in games if _base_min(g) >= 24]
    rot = [g for g in games if _base_min(g) < 24]
    print(f"  consistent starter (baseline >=24 min, can't grow): {_grec(starter)}   <- over-projection risk")
    print(f"  rotation player   (baseline <24 min, room to grow): {_grec(rot)}")
    both = [g for g in games if (g["d_min"] or 0) >= 2 and (g["d_fga"] or 0) >= 1]
    print(f"  both bumps (minutes +2 AND usage +1):               {_grec(both)}")
    sweet = [g for g in games if _base_min(g) < 24 and (g["d_min"] or 0) >= 2 and (g["d_fga"] or 0) >= 1]
    print(f"  ARCHETYPE: rotation + both bumps:                   {_grec(sweet)}  ({', '.join(g['player'].split()[-1] for g in sweet)})")
    # NOTE prior framings that DIED: 'role-player dream-jump by scoring avg' was contradicted (role
    # players won 2-0); the 'elevation 3-5 sweet spot' was a laddering illusion (7-1 -> 4-1 deduped).


def haircut_menu():
    pts = _overs("stat='points' AND elev_avg IS NOT NULL")
    fwd = [r for r in pts if (r["pred_date"] or "") >= EPOCH]
    print(f"HAIRCUT SHADOW MENU (parked — kept for the forward test; drop = haircut proj <= line)")
    for slc, lbl in [(pts, "full (in-sample-heavy)"), (fwd, f"forward >= {EPOCH}")]:
        if not slc:
            print(f"  {lbl}: n=0")
            continue
        print(f"  {lbl}: no-haircut {_rec(slc)}")
        for name, fn in HAIRCUTS.items():
            kept = [r for r in slc if fn(r["elev_avg"]) > r["line"]]
            print(f"    {name:>6} kept {_rec(kept)}")


def selected_by_baseline():
    # THE RIGHT POPULATION (user's correction): not every projection, only plays the model actually
    # SELECTED & bet (all stats), split by baseline minutes, at the real-bet (line-vs-actual) level.
    rows = _overs("proj_min IS NOT NULL AND d_min IS NOT NULL")
    b = lambda r: (r["proj_min"] or 0) - (r["d_min"] or 0)
    print(f"SELECTED & BET plays by BASELINE MINUTES  [all stats, n={len(rows)} — the right population]")
    for lbl, lo, hi in [("rotation <20", 0, 20), ("tweener 20-24", 20, 24), ("starter >=24", 24, 99)]:
        print(f"  {lbl:14} {_rec([r for r in rows if lo <= b(r) < hi])}")
    print("  => baseline minutes is a WASH at the bet level (rotation ~53% == starter ~52%).")


def projection_bias():
    # THE LARGER, HONEST SAMPLE: wnba_proj_log grades proj-vs-actual for EVERY projection (incl.
    # never-bet), ~110 rows vs 14 bet games. It settled the role-expansion debate: LOW-baseline
    # players are the OVER-projected group, not the starters (the bet sample was survivorship).
    import statistics as st
    plog = HERE / "wnba_proj_log.sqlite"
    if not plog.exists():
        print("PROJECTION BIAS: wnba_proj_log.sqlite not found")
        return
    con = sqlite3.connect(plog)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT d_min,proj_min,proj_pts,actual_pts,actual_min FROM projections "
        "WHERE proj_pts IS NOT NULL AND actual_pts IS NOT NULL AND proj_min IS NOT NULL AND d_min IS NOT NULL")]
    con.close()
    base = lambda r: (r["proj_min"] or 0) - (r["d_min"] or 0)
    print(f"POINTS PROJECTION BIAS by baseline minutes  [proj_log, n={len(rows)} — the honest sample]")
    for lbl, lo, hi in [("rotation/bench <20", 0, 20), ("tweener 20-24", 20, 24), ("starter >=24", 24, 99)]:
        g = [r for r in rows if lo <= base(r) < hi]
        if not g:
            continue
        bias = st.mean([r["proj_pts"] - r["actual_pts"] for r in g])
        reach = sum(1 for r in g if r["actual_pts"] >= r["proj_pts"]) / len(g) * 100
        mgap = st.mean([r["proj_min"] - r["actual_min"] for r in g if r["actual_min"]])
        print(f"  {lbl:20} n={len(g):3}  pts bias {bias:+.1f}  reach {reach:2.0f}%  phantom-min {mgap:+.1f}")
    print("  => LOW-baseline players are OVER-projected (phantom minutes); starters are calibrated.")
    print("  => the bet sample's 'rotation 4-0' was survivorship (soft lines), not projection skill.")


def report():
    print("=" * 68)
    per_prop()
    print("-" * 68)
    points_regime()
    print("-" * 68)
    selected_by_baseline()
    print("-" * 68)
    projection_bias()
    print("-" * 68)
    hypotheses()
    print("-" * 68)
    haircut_menu()
    print("=" * 68)
    print("VERDICT: band gate already handles the points leak; haircut is parked.")
    print("The bet sample's 'role-expansion' story (rotation players are the edge) was")
    print("REFUTED by the 110-row proj_log: LOW-baseline players are the OVER-projected")
    print("group (+1.8 pts, +3.5 phantom minutes, reach proj 28%); starters are calibrated")
    print("(+0.4, 40%). The 4-0 was survivorship on soft lines. SHIP NOTHING on n<~100")
    print("player-games — every small-sample story here has reversed under more data.")


if __name__ == "__main__":
    report()
