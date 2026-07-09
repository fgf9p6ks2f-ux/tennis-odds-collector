"""Individualized pitcher split data (statsapi, one cheap call each) + park K-factors.

For the strikeout study: does conditioning on a pitcher's OWN home/away, day/night, or
platoon split — or the park — add predictive signal beyond the base model? These pullers
provide the inputs; k_characteristics measures whether they actually matter (most split
variation at half-season samples is noise, so this is a measure-don't-assume exercise).
"""
from __future__ import annotations

from . import data

_get = data._get


def pitcher_splits(pid: int, season: int) -> dict:
    """{ 'home':(k,bf), 'away':.., 'day':.., 'night':.., 'vL':.., 'vR':.. } K/BF splits."""
    r = _get(f"/people/{pid}/stats", stats="statSplits", group="pitching",
             season=season, sitCodes="h,a,d,n,vl,vr")
    code = {"h": "home", "a": "away", "d": "day", "n": "night", "vl": "vL", "vr": "vR"}
    out = {}
    for s in (r.get("stats") or [{}])[0].get("splits", []):
        c = code.get(s.get("split", {}).get("code"))
        st = s["stat"]
        bf = st.get("battersFaced") or 0
        if c and bf:
            out[c] = (st.get("strikeOuts") or 0, bf)
    return out


def park_k_factors(season: int) -> tuple[dict[int, float], float]:
    """({team_id: park K-factor}, league_avg=1.0). Isolates the PARK by comparing a team's
    own pitching staff's K/BF at home vs away — same pitchers, different venue, so the
    ratio ~ the park's effect on strikeouts. >1 = K-friendly park, <1 = suppressor (Coors).
    Blended 50% toward 1.0 (half-season noise) so it never dominates the projection."""
    home = _get("/teams/stats", stats="statSplits", group="pitching", season=season,
                sportId=1, sitCodes="h")
    away = _get("/teams/stats", stats="statSplits", group="pitching", season=season,
                sportId=1, sitCodes="a")

    def rates(blob):
        out = {}
        for t in blob["stats"][0]["splits"]:
            st = t["stat"]
            bf = st.get("battersFaced") or 0
            if bf:
                out[t["team"]["id"]] = (st.get("strikeOuts") or 0) / bf
        return out

    h, a = rates(home), rates(away)
    factors = {}
    for tid in h:
        if tid in a and a[tid] > 0:
            raw = h[tid] / a[tid]
            factors[tid] = 1.0 + 0.5 * (raw - 1.0)      # shrink half toward neutral
    return factors, 1.0
