"""Injury-regime DIVERGENCE flag — display-only, NOT a projection change.

The regime-conditional PROJECTION backtested as a wash: recency already captures a stable role,
and the exact injury combination is too rare (only ~15% of injury spots have >=3 exact comps) to
model reliably. But the ONE thing this computes dependably is: does tonight's absence set match
the player's RECENT games? When it doesn't — a driver just returned, or a fresh injury — recency
is drawing from the wrong regime, and that's exactly where a human "find the closest comp" read
beats the model (you can eyeball whether the comp sample is clean; the model can't).

So this surfaces, for CONSISTENT-minutes players only (fringe players' comps are too noisy to
trust): (1) whether tonight diverges from recent games, and (2) the closest historical comps —
the games where the same high-impact teammates were out — with the player's line in them. The
match is weighted by each absent teammate's role size, so it keys on the Rice/Sykes-level
absences, not a bench end who sat.
"""
import datetime as dt
import statistics as st

KEY = {"points": "pts", "rebounds": "reb", "assists": "ast"}


def _played(log):
    return {g["date"][:10] for g in log if (g.get("min") or 0) > 0}


def _active_around(dates, d, win=24):
    """Teammate is a real rotation-mate AROUND date d (played within +/-win days) — so their absence
    in game d is a genuine INJURY/DNP, not 'not on the team yet'. Symmetric, so a star injured at the
    season OPENER (no prior games, but games soon after) still counts as a real absence — the old
    before-only window silently dropped every early-season both-out comp."""
    d0 = dt.date.fromisoformat(d)
    lo, hi = (d0 - dt.timedelta(days=win)).isoformat(), (d0 + dt.timedelta(days=win)).isoformat()
    return any(lo <= x <= hi and x != d for x in dates)


def _role(log):
    m = [g["min"] for g in log if (g.get("min") or 0) > 0]
    return st.mean(m) if m else 0.0


def regime_note(blog, out_logs, out_names, stat, in_logs=None, in_names=None):
    """Closest COMPS conditioned on the FULL impact lineup — the ruled-out players ABSENT *and* the
    key in-players PRESENT. Basketball usage is defined by 2-3+ impact players, not one: Johannes
    with Sabally+Fiebich out AVERAGES 13.7, but that's 15.8 with Ionescu ALSO out vs 9.5 with
    Ionescu IN — so a comp that ignores who's on the floor is misleading. We weight every impact
    teammate by role size and score each historical game by how much of tonight's exact in/out
    configuration it reproduces; comp_avg is over the games that match tonight BEST.

    blog: beneficiary log. out_logs/out_names: tonight's ruled-out teammates. in_logs/in_names:
    the team's OTHER impact players expected to PLAY tonight (Ionescu, Stewart...). stat: the market.
    Returns a dashboard dict, or None when it doesn't apply."""
    key = KEY.get(stat, stat)
    games = sorted((g for g in blog if (g.get("min") or 0) > 0), key=lambda g: g["date"])
    if len(games) < 6 or not out_logs or max(g["min"] for g in games) < 18:
        return None
    in_logs, in_names = in_logs or [], in_names or []
    out_played = [_played(ol) for ol in out_logs]
    in_played = [_played(il) for il in in_logs]
    wt_out = [_role(ol) for ol in out_logs]
    wt_in = [_role(il) for il in in_logs]
    if max(wt_out, default=0) <= 0:
        return None
    # key config members = role >= half the biggest OUT's minutes (screens out bench-end noise on
    # both sides). sig_out defines the absences; sig_in the presences that suppress a usage spike.
    thr = 0.5 * max(wt_out)
    sig_out = [i for i in range(len(out_logs)) if wt_out[i] >= thr]
    sig_in = [j for j in range(len(in_logs)) if wt_in[j] >= thr]
    if not sig_out:
        return None
    sig_names = [out_names[i] for i in sig_out if i < len(out_names)]
    inn = [in_names[j] for j in sig_in if j < len(in_names)]
    tw = (sum(wt_out[i] for i in sig_out) + sum(wt_in[j] for j in sig_in)) or 1.0

    def match(d):     # role-weighted share of tonight's config (outs absent + ins present) on date d
        s = 0.0
        for i in sig_out:
            if d not in out_played[i] and _active_around(out_played[i], d):
                s += wt_out[i]
        for j in sig_in:
            if d in in_played[j]:
                s += wt_in[j]
        return s / tw

    rows = [(g, match(g["date"][:10])) for g in games]
    close = [r for r in sorted(rows, key=lambda r: r[1], reverse=True) if r[1] >= 0.55][:6]
    if len(close) < 2 or st.median([r[0]["min"] for r in close]) < 15:
        return None
    # comp_avg over the games matching tonight BEST (within 0.12 of the top match) so the Ionescu-IN
    # games drive the number tonight, not the Ionescu-OUT blowouts that inflate the raw split.
    best = close[0][1]
    primary = [r for r in close if r[1] >= best - 0.08]        # near-exact config matches only
    disp = sorted(close, key=lambda r: r[0]["date"], reverse=True)
    comps = [{"date": r[0]["date"][:10], "opp": (r[0].get("matchup") or ""),
              "min": round(r[0]["min"]), "val": round(r[0].get(key, 0)), "match": round(r[1], 2)}
             for r in disp]
    recent_match = st.mean(r[1] for r in rows[-3:])
    return {"divergent": recent_match < 0.55,                 # recent games miss tonight's config
            "recent_match": round(recent_match, 2), "sig_names": sig_names, "in_names": inn,
            "comps": comps, "comp_avg": round(st.mean(r[0].get(key, 0) for r in primary), 1),
            "n_comps": len(comps), "n_primary": len(primary)}
