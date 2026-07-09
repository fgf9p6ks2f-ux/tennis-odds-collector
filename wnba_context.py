"""WNBA matchup context — Vegas total, pace, and opponent defense, all from ESPN.

Three signals the injury edge is blind to on its own:
  - total  : the Vegas over/under (straight off ESPN's DraftKings line) — the scoring
             environment; a high total means points are cheap for everyone.
  - pace   : avg combined points in each team's games (a possessions proxy) — more
             possessions => more of EVERY counting stat (the user's key ask).
  - opp_def: points the opponent allows per game — DvP for points (weak D => boost).

All from ESPN (schedule finals + scoreboard odds), so it runs in CI — unlike the old
stats.nba DvP, which the datacenter block killed. Team pace/def are cached to disk (they
move slowly); the game lines are fetched live (they move through the day).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CACHE = Path(__file__).resolve().parent / "wnba_context_cache.json"
ET = dt.timezone(dt.timedelta(hours=-4))


def _get(path):
    for _ in range(3):
        try:
            r = requests.get(f"{SITE}/{path}", headers=H, timeout=20)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1)
    return {}


def _score(x):
    s = x.get("score")
    try:
        return float(s.get("value")) if isinstance(s, dict) else float(s or 0)
    except (TypeError, ValueError):
        return 0.0


def game_lines():
    """{team: {opp, total, spread, spread_mag}} for today's (ET) slate — Vegas O/U +
    spread from ESPN's odds feed. total/spread are None when the book hasn't posted yet."""
    et = dt.datetime.now(dt.timezone.utc).astimezone(ET).strftime("%Y%m%d")
    out = {}
    for e in _get(f"scoreboard?dates={et}").get("events", []):
        comp = e.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) != 2:
            continue
        ab = [c["team"]["abbreviation"] for c in cs]
        odds = (comp.get("odds") or [{}])[0]
        total = odds.get("overUnder")
        details = odds.get("details", "")                 # e.g. "PHX -1.5"
        m = re.search(r"-?\d+\.?\d*", details or "")
        mag = abs(float(m.group())) if m else None
        for i, a in enumerate(ab):
            out[a] = {"opp": ab[1 - i], "total": total, "spread": details, "spread_mag": mag}
    return out


def team_rates(max_age_h=12):
    """{abbr: {ppg, oppg, pace, n}} from schedule finals. Cached (pace/def move slowly)."""
    now = dt.datetime.now(dt.timezone.utc)
    if CACHE.exists():
        try:
            d = json.loads(CACHE.read_text())
            if (now - dt.datetime.fromisoformat(d["ts"])).total_seconds() / 3600 < max_age_h:
                return d["rates"]
        except (ValueError, KeyError):
            pass
    teams = _get("teams").get("sports", [{}])[0]["leagues"][0]["teams"]
    rates = {}
    for t in teams:
        tm = t["team"]
        abbr = tm["abbreviation"]
        forp = agn = n = 0
        for e in _get(f"teams/{tm['id']}/schedule").get("events", []):
            c = e.get("competitions", [{}])[0]
            if c.get("status", {}).get("type", {}).get("state") != "post":
                continue
            cs = c.get("competitors", [])
            mine = [x for x in cs if x["team"]["abbreviation"] == abbr]
            opp = [x for x in cs if x["team"]["abbreviation"] != abbr]
            if not mine or not opp:
                continue
            forp += _score(mine[0])
            agn += _score(opp[0])
            n += 1
        if n:
            rates[abbr] = {"ppg": round(forp / n, 1), "oppg": round(agn / n, 1),
                           "pace": round((forp + agn) / n, 1), "n": n}
        time.sleep(0.05)
    CACHE.write_text(json.dumps({"ts": now.isoformat(), "rates": rates}))
    return rates


def matchup_context(team, opp, lines=None, rates=None):
    """{total, spread, spread_mag, opp_pts_allowed, pace, pace_vs_lg} for `team` vs `opp`.
    Pass pre-fetched lines/rates to avoid refetching per beneficiary."""
    lines = lines if lines is not None else game_lines()
    rates = rates if rates is not None else team_rates()
    ln = lines.get(team, {})
    r_opp, r_me = rates.get(opp, {}), rates.get(team, {})
    pace = round((r_opp["pace"] + r_me["pace"]) / 2, 1) if r_opp.get("pace") and r_me.get("pace") else None
    lg_avg = round(sum(v["pace"] for v in rates.values()) / len(rates), 1) if rates else None
    return {"total": ln.get("total"), "spread": ln.get("spread"), "spread_mag": ln.get("spread_mag"),
            "opp_pts_allowed": r_opp.get("oppg"), "pace": pace,
            "pace_vs_lg": round(pace - lg_avg, 1) if (pace and lg_avg) else None}


def matchup_note(team, opp, lines=None, rates=None):
    """Short human tag for the board/alert, e.g. 'O/U 174.5 · PHX allows 86 · fast'."""
    if not opp:
        return ""
    c = matchup_context(team, opp, lines, rates)
    bits = []
    if c["total"]:
        bits.append(f"O/U {c['total']}")
    if c["opp_pts_allowed"]:
        bits.append(f"{opp} allows {c['opp_pts_allowed']:g}")
    if c["pace_vs_lg"] is not None:
        bits.append("fast" if c["pace_vs_lg"] > 2 else "slow" if c["pace_vs_lg"] < -2 else "avg pace")
    return " · ".join(bits)
