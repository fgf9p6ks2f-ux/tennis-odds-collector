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


def _active_before(dates, d, win=24):
    lo = (dt.date.fromisoformat(d) - dt.timedelta(days=win)).isoformat()
    return any(lo <= x < d for x in dates)


def regime_note(blog, out_logs, out_names, stat):
    """blog: beneficiary game log. out_logs: tonight's ruled-out teammates' logs. out_names: their
    short names (aligned to out_logs). stat: 'points'|'rebounds'|'assists'. Returns a dict for the
    dashboard, or None when it doesn't apply (fringe minutes / no comps / no injuries)."""
    key = KEY.get(stat, stat)
    games = sorted((g for g in blog if (g.get("min") or 0) > 0), key=lambda g: g["date"])
    if len(games) < 6 or not out_logs:
        return None
    # consistent-minutes gate: only trust comps for a real rotation player
    mins = [g["min"] for g in games]
    if st.median(mins) < 16 or sum(1 for m in mins if m >= 12) / len(mins) < 0.7:
        return None
    outd = [_played(ol) for ol in out_logs]
    # role weight per absent teammate = mean minutes when active (impact size). Match keys on the
    # HIGH-impact absences only (>= half the biggest out's minutes) so bench-end outs don't dilute
    # it — a Rice/Sykes-out game shouldn't score low just because a 10-min reserve also sits.
    wt = []
    for ol in out_logs:
        played = [g["min"] for g in ol if (g.get("min") or 0) > 0]
        wt.append(st.mean(played) if played else 0.0)
    if max(wt) <= 0:
        return None
    thr = 0.5 * max(wt)
    sig = [i for i in range(len(out_logs)) if wt[i] >= thr]    # the absences that define the regime
    tw = sum(wt[i] for i in sig) or 1.0
    sig_names = [out_names[i] for i in sig if i < len(out_names)]

    def match(d):     # role-weighted share of the KEY absences that also applied on date d
        return sum(wt[i] for i in sig if d not in outd[i] and _active_before(outd[i], d)) / tw

    rows = [(g, match(g["date"][:10])) for g in games]
    # the CLOSEST available analogues to tonight — top matches (>=0.5 share of the key absences),
    # up to 6. Not a hard exact-match cut: when the precise combo is rare we still surface the
    # nearest games rather than collapse to one. comp_avg is over exactly these.
    close = [r for r in sorted(rows, key=lambda r: r[1], reverse=True) if r[1] >= 0.5][:6]
    if len(close) < 2:                                        # need a couple of real comps to trust
        return None
    disp = sorted(close, key=lambda r: r[0]["date"], reverse=True)
    comps = [{"date": r[0]["date"][:10], "opp": (r[0].get("matchup") or ""),
              "min": round(r[0]["min"]), "val": round(r[0].get(key, 0)), "match": round(r[1], 2)}
             for r in disp]
    recent_match = st.mean(r[1] for r in rows[-3:])
    return {"divergent": recent_match < 0.55,                 # recent games miss the KEY absences
            "recent_match": round(recent_match, 2), "sig_names": sig_names,
            "comps": comps, "comp_avg": round(st.mean(r[0].get(key, 0) for r in close), 1),
            "n_comps": len(comps)}
