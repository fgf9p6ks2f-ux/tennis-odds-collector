"""Opponent- and pace-adjusted DvP (Defense vs Position) for WNBA, from ESPN box scores.

Ridge (RAPM-style): each position-P player-game's PACE-ADJUSTED per-minute rate is decomposed
    rate = league_mean(P) + player_offense + opponent_defense
with L2 shrinkage, so the opponent_defense coefficients are a team's true tendency to allow
stat S to position P, net of schedule and pace. Backtested (dvp_backtest.py): a real but SMALL
matchup signal — it correctly orders overs by matchup but doesn't clear break-even alone and is
flat on unders. So it's used as a light TIEBREAKER + display note, never a core driver. Cached
daily to wnba_dvp_cache.json (recompute is ~a season of box scores, disk-cached by wnba_pbp).

    dvp(team, pos, stat)          -> coefficient in stat-units per projected minute (+ = softer)
    matchup_note(team, pos, stat) -> 'soft' | 'tough' | None   (for the card / alert)
"""
from __future__ import annotations

import datetime as dt
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

try:
    import numpy as np                # only needed to FIT the DvP model (a separate --fit step); the
except ImportError:                   # alert READS the cached JSON, so a missing numpy must never
    np = None                         # crash module import and take down the whole alert pipeline.

import wnba_pbp as P
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
CACHE = HERE / "wnba_dvp_cache.json"
STATS = ("reb", "pts", "ast", "fg3m")
_PG = {"G": "G", "PG": "G", "SG": "G", "GF": "G", "F": "F", "SF": "F", "PF": "F",
       "FC": "C", "C": "C", "CF": "C"}
LAMBDA = 60.0
MIN_MIN = 8.0
SEASON_DAYS = 130
_MEM = {}


def _sess():
    from curl_cffi import requests as cr
    return cr.Session(impersonate="chrome")


def _game_ids(days):
    s = _sess()
    now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))
    out = []
    for d in range(days):
        date = (now - dt.timedelta(days=d)).strftime("%Y%m%d")
        try:
            j = s.get("https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/"
                      f"scoreboard?dates={date}", timeout=20).json()
        except Exception:
            continue
        for e in j.get("events", []):
            if e.get("status", {}).get("type", {}).get("state") == "post":
                out.append(e["id"])
    return out


def positions():
    """({pid: G/F/C}, {valid WNBA team abbreviations}) — the roster teams ARE the real WNBA
    teams, so their abbreviations whitelist out international/exhibition games (NIGER, JPN...)
    that ESPN's scoreboard picks up during the Olympic break."""
    out, teamset = {}, set()
    teams = W._get(f"{W.SITE}/teams").get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    for t in teams:
        ab = (t.get("team") or {}).get("abbreviation")
        if ab:
            teamset.add(ab)
        for a in W._get(f"{W.SITE}/teams/{t['team']['id']}/roster").get("athletes", []):
            if a.get("id"):
                out[a["id"]] = _PG.get((a.get("position") or {}).get("abbreviation", ""), "F")
    return out, teamset


def _boxscore(gid):
    box = P.fetch(gid).get("boxscore", {})
    teams, poss, order = {}, {}, []
    for tm in box.get("players", []):
        team = (tm.get("team") or {}).get("abbreviation")
        if team not in teams:
            teams[team], poss[team] = {}, 0.0
            order.append(team)
        for stt in tm.get("statistics", []):
            keys = stt.get("keys") or []

            def gi(k):
                return keys.index(k) if k in keys else None

            im, ip, ir, ia = gi("minutes"), gi("points"), gi("rebounds"), gi("assists")
            i3 = gi("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
            ifga = gi("fieldGoalsMade-fieldGoalsAttempted")
            ifta = gi("freeThrowsMade-freeThrowsAttempted")
            itov, ioreb = gi("turnovers"), gi("offensiveRebounds")
            for a in stt.get("athletes", []):
                pid = a.get("athlete", {}).get("id")
                s = a.get("stats") or []
                if not pid or not s:
                    continue

                def num(i):
                    try:
                        return float(s[i]) if i is not None else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                def made(i):
                    try:
                        return float(s[i].split("-")[0]) if i is not None else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                def att(i):
                    try:
                        return float(s[i].split("-")[1]) if i is not None else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                mn = num(im)
                poss[team] += att(ifga) + 0.44 * att(ifta) - num(ioreb) + num(itov)
                if mn > 0:
                    teams[team][pid] = {"min": mn, "pts": num(ip), "reb": num(ir),
                                        "ast": num(ia), "fg3m": made(i3)}
    if len(order) != 2:
        return []
    a, b = order
    return [(pid, team, opp, poss[team], r)
            for team, opp in ((a, b), (b, a)) for pid, r in teams[team].items()]


def _fit(rows, lam=LAMBDA):
    if np is None:                     # numpy absent — skip the fit; dvp() falls back to the cache/0
        return {}
    pids = sorted({r[0] for r in rows})
    teams = sorted({r[1] for r in rows})
    if len(rows) < 40 or not teams:
        return {}
    pi = {p: i for i, p in enumerate(pids)}
    ti = {t: i for i, t in enumerate(teams)}
    npl = len(pids)
    p = 1 + npl + len(teams)
    X = np.zeros((len(rows), p))
    y = np.array([r[2] for r in rows])
    X[:, 0] = 1.0
    for i, (pid, team, _) in enumerate(rows):
        X[i, 1 + pi[pid]] = 1.0
        X[i, 1 + npl + ti[team]] = 1.0
    reg = np.ones(p)
    reg[0] = 0.0
    beta = np.linalg.solve(X.T @ X + lam * np.diag(reg), X.T @ y)
    return {t: round(float(beta[1 + npl + ti[t]]), 4) for t in teams}


def compute():
    id2pos, wnba = positions()
    allg = []
    lg_poss = []
    for gid in _game_ids(SEASON_DAYS):
        try:
            rows = _boxscore(gid)
        except Exception:
            continue
        for pid, team, opp, tposs, r in rows:
            if team not in wnba or opp not in wnba:      # skip exhibitions / national teams
                continue
            allg.append((pid, team, opp, tposs, r))
            lg_poss.append(tposs)
    lg = st.mean(lg_poss) if lg_poss else 80.0
    table = {}
    for stat in STATS:
        for P_ in ("G", "F", "C"):
            rows = [(pid, opp, (r[stat] / r["min"]) * (lg / max(tposs, 1)))
                    for pid, team, opp, tposs, r in allg
                    if id2pos.get(pid) == P_ and r["min"] >= MIN_MIN]
            fit = _fit(rows)
            if fit:
                table[f"{stat}|{P_}"] = fit
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    CACHE.write_text(json.dumps({"date": today, "lg_pace": round(lg, 1), "table": table}))
    return table


def dvp_table():
    if _MEM.get("table") is not None:
        return _MEM["table"]
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if CACHE.exists():
        try:
            c = json.loads(CACHE.read_text())
            if c.get("date") == today:
                _MEM["table"] = c["table"]
                return c["table"]
        except Exception:
            pass
    try:
        t = compute()
    except Exception:
        t = (json.loads(CACHE.read_text()).get("table", {}) if CACHE.exists() else {})
    _MEM["table"] = t
    return t


def dvp(team, pos, stat):
    """Opponent-adjusted DvP coefficient (stat-units per minute; + = opponent allows more)."""
    return dvp_table().get(f"{stat}|{_PG.get(pos, pos)}", {}).get(team, 0.0)


def matchup_note(team, pos, stat):
    coefs = sorted(dvp_table().get(f"{stat}|{_PG.get(pos, pos)}", {}).values())
    c = dvp(team, pos, stat)
    if not coefs or len(coefs) < 6 or c == 0:
        return None
    if c >= coefs[-3]:
        return "soft"
    if c <= coefs[2]:
        return "tough"
    return None


if __name__ == "__main__":
    t = compute()
    print(f"DvP computed: {len(t)} (stat|pos) tables cached to {CACHE.name}")
    for key in ("pts|G", "reb|C", "fg3m|G"):
        d = t.get(key, {})
        if d:
            r = sorted(d.items(), key=lambda kv: kv[1])
            print(f"  {key}: softest {r[-2:][::-1]}  toughest {r[:2]}")
