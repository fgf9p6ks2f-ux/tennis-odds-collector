"""Leak-free backtest of the 3 WNBA projection layers over the last N days.

For every player-game G, project the stat using ONLY that player's PRIOR games, three ways,
and score each against what actually happened in G (projection MAE — lower = better):
  OLD  : raw mean of the player's elevated-role (min>=22) prior games  [what shipped before]
  MH   : + minutes-honest (scale each prior game to the projected minutes)
  CTX  : + context-weighting (weight prior games by how well their lineup matched G's)

The lineup of G (who played + their minutes) is known pre-game from the confirmed lineup, so
using it as tonight's context is NOT a leak. Projected minutes = the player's trailing average
(also pre-game).

Scored the way BETTING actually works — being on the right side of the line, not dead-on:
  · CALIBRATION : how often actual landed above our number (50% = unbiased; <50% = we run high)
  · OVER hit %  : set the line at the season average (the stale book number), bet OVER when the
                  projection clears it -> did the actual clear it too? (break-even @ -110 = 52.4%)
  · UNDER hit % : symmetric
  · MAE         : closeness, shown last, because it does NOT reward being on the right side

    python wnba_backtest_layers.py [days]
"""
from __future__ import annotations

import datetime as dt
import math
import statistics as st
import sys
from collections import defaultdict

import wnba_pbp as P
import wnba_wowy as W

ROLE_FLOOR = 22
STATS = ("reb", "pts", "ast")
_PG = {"G": "G", "PG": "G", "SG": "G", "GF": "G", "F": "F", "SF": "F", "PF": "F",
       "FC": "C", "C": "C", "CF": "C"}
_STAT_CTX = {"pts": ("G", "F", "C"), "reb": ("C", "F"), "ast": ("G",)}


def _sess():
    from curl_cffi import requests as cr
    return cr.Session(impersonate="chrome")


def game_ids(days):
    """[(game_id, date)] for FINAL games in the last `days` days (ET)."""
    s = _sess()
    now_et = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))
    out = []
    for d in range(days + 2):
        date = (now_et - dt.timedelta(days=d)).strftime("%Y%m%d")
        try:
            j = s.get("https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/"
                      f"scoreboard?dates={date}", timeout=20).json()
        except Exception:
            continue
        iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        for e in j.get("events", []):
            if e.get("status", {}).get("type", {}).get("state") == "post":
                out.append((e["id"], iso))
    return out


def boxscore(gid):
    """[ {pid, team, min, pts, reb, ast} ] via the cached PBP summary (disk-cached)."""
    box = P.fetch(gid).get("boxscore", {})
    rows = []
    for tm in box.get("players", []):
        team = tm.get("team", {}).get("abbreviation")
        for stt in tm.get("statistics", []):
            keys = stt.get("keys") or []
            idx = {k: keys.index(k) for k in ("minutes", "points", "rebounds", "assists")
                   if k in keys}
            for a in stt.get("athletes", []):
                pid = a.get("athlete", {}).get("id")
                stats = a.get("stats") or []
                if not pid or not stats:
                    continue

                def num(k):
                    try:
                        return float(stats[idx[k]]) if k in idx else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                if num("minutes") > 0:
                    rows.append({"pid": pid, "team": team, "min": num("minutes"),
                                 "pts": num("points"), "reb": num("rebounds"),
                                 "ast": num("assists")})
    return rows


def build_history(fetch_days):
    """{pid: [game dicts chronological]}; each game carries `lineup` = {teammate_pid: minutes}."""
    hist = defaultdict(list)
    for gid, date in game_ids(fetch_days):
        try:
            rows = boxscore(gid)
        except Exception:
            continue
        if not rows:
            continue
        by_team = defaultdict(dict)
        for r in rows:
            by_team[r["team"]][r["pid"]] = r["min"]
        for r in rows:
            r["date"] = date
            r["lineup"] = {p: m for p, m in by_team[r["team"]].items() if p != r["pid"]}
            hist[r["pid"]].append(r)
    for pid in hist:
        hist[pid].sort(key=lambda g: g["date"])
    return hist


def project(prior, proj_min, stat, ctx_lineup, id2pos):
    """(OLD, MH, CTX, season_avg) projections of `stat` from the player's prior games."""
    floor = max(proj_min - 4, ROLE_FLOOR)
    elev = [g for g in prior if g["min"] >= floor]
    if len(elev) < 4:
        return None
    season_avg = st.mean(g[stat] for g in prior)     # book's anchor ~ the season number
    old = st.mean(g[stat] for g in elev)
    vals = [g[stat] * min(proj_min / max(g["min"], 1), 1.35) for g in elev]
    mh = st.mean(vals)
    # context: weight each prior elevated game by how closely the position-relevant competitors'
    # minutes matched tonight (ctx_lineup = {pid: minutes} in G)
    want = _STAT_CTX.get(stat, ("G", "F", "C"))
    comp = {p: m for p, m in ctx_lineup.items() if _PG.get(id2pos.get(p, "F"), "F") in want}
    if not comp:
        return old, mh, mh, season_avg
    ws = []
    for g in elev:
        wt = 1.0
        for p, tgt in comp.items():
            wt *= math.exp(-((g["lineup"].get(p, 0.0) - tgt) / 12.0) ** 2)
        ws.append(wt)
    wsum = sum(ws)
    if wsum <= 0:
        return old, mh, mh, season_avg
    wmean = sum(v * wt for v, wt in zip(vals, ws)) / wsum
    eff = wsum * wsum / sum(wt * wt for wt in ws)
    ctx = (wmean * eff + mh * 5.0) / (eff + 5.0)
    return old, mh, ctx, season_avg


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    id2pos = {v["id"]: v["position"] for v in W.players().values()}
    hist = build_history(days + 18)          # fetch wider history, only SCORE the last `days`
    cutoff = (dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))
              - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    METHODS = ("OLD", "MH", "CTX")
    LABEL = {"OLD": "OLD", "MH": "MINUTES-HONEST", "CTX": "+CONTEXT"}
    err = {m: defaultdict(list) for m in METHODS}                     # |proj - actual|  (MAE)
    abov = {m: 0 for m in METHODS}                                    # # times actual > proj (calibration)
    ov = {m: {s: {"n": 0, "w": 0} for s in STATS} for m in METHODS}   # OVER bets at the stale line, per stat
    un = {m: {s: {"n": 0, "w": 0} for s in STATS} for m in METHODS}   # UNDER bets, per stat
    base = {s: {"n": 0, "over": 0} for s in STATS}                    # blind base rate of clearing the line
    n_games = 0
    for pid, games in hist.items():
        for i, g in enumerate(games):
            if g["date"] < cutoff:           # only score games inside the 2-week test window
                continue
            prior = games[:i]
            if len(prior) < 5:
                continue
            proj_min = st.mean(x["min"] for x in prior[-5:])
            for stat in STATS:
                p = project(prior, proj_min, stat, g["lineup"], id2pos)
                if not p:
                    continue
                old, mh, ctx, savg = p
                a = g[stat]
                # the book anchors the line near the SEASON average (it's slow to price a role
                # change — that's the edge). A .5 line just off the season number, so integer
                # actuals never push.
                L = math.floor(savg) + 0.5
                base[stat]["n"] += 1
                base[stat]["over"] += 1 if a > L else 0
                for m, pr in (("OLD", old), ("MH", mh), ("CTX", ctx)):
                    err[m][stat].append(abs(pr - a))
                    if a > pr:
                        abov[m] += 1
                    if pr >= L + 0.5:                 # model sees clear elevation -> bet OVER
                        ov[m][stat]["n"] += 1
                        ov[m][stat]["w"] += 1 if a > L else 0
                    elif pr <= L - 0.5:               # model sees suppression -> bet UNDER
                        un[m][stat]["n"] += 1
                        un[m][stat]["w"] += 1 if a < L else 0
                n_games += 1

    def agg(d):                                          # sum a per-stat {n,w} dict -> hit %
        n = sum(d[s]["n"] for s in STATS)
        w = sum(d[s]["w"] for s in STATS)
        return n, (100 * w / n if n else float("nan"))

    bn = sum(base[s]["n"] for s in STATS)
    bov = sum(base[s]["over"] for s in STATS)
    base_over = 100 * bov / bn if bn else 0                          # blind: clear the line
    base_under = 100 - base_over                                     # blind: stay under
    base_over_s = {s: 100 * base[s]["over"] / base[s]["n"] if base[s]["n"] else 0 for s in STATS}
    tot = {m: sum(len(err[m][s]) for s in STATS) for m in METHODS}
    print(f"\nBACKTEST — last {days} days, {n_games} player-stat projections "
          f"(line = season avg, so 'over' = we bet the elevated role beats the stale number)\n")

    print(f"  0. BASELINE — bet EVERY spot blindly (controls for stat skew: counting stats are")
    print(f"     right-skewed, so 'under the average' wins even with zero skill):")
    print(f"     blind OVER {base_over:.1f}%   |   blind UNDER {base_under:.1f}%   "
          f"(this is the bar every method must BEAT)\n")

    print("  1. CALIBRATION — how often the actual landed ABOVE our projection (50% = unbiased;")
    print("     <50% = we project too HIGH, so overs at our number lose):")
    for m in METHODS:
        pct = 100 * abov[m] / tot[m] if tot[m] else 0
        flag = "  <-- over-projects" if pct < 45 else ("  <-- well-centered" if 47 <= pct <= 53 else "")
        print(f"     {LABEL[m]:16}{pct:5.1f}%{flag}")

    print(f"\n  2. OVER bets (proj clears the line by 1+). Raw hit %, and EDGE vs the {base_over:.0f}% blind base:")
    print(f"     {'method':16}{'# bets':>8}{'hit %':>9}{'vs base':>10}")
    for m in METHODS:
        n, r = agg(ov[m])
        e = r - base_over
        tag = "  real edge" if e > 2 else ("  no edge" if abs(e) <= 2 else "  WORSE than blind")
        print(f"     {LABEL[m]:16}{n:>8}{r:>8.1f}%{e:>+9.1f}{tag}")

    print(f"\n  3. UNDER bets (proj below the line by 1+). Compare to the {base_under:.0f}% blind base:")
    print(f"     {'method':16}{'# bets':>8}{'hit %':>9}{'vs base':>10}")
    for m in METHODS:
        n, r = agg(un[m])
        e = r - base_under
        tag = "  real edge" if e > 2 else ("  no edge" if abs(e) <= 2 else "  WORSE than blind")
        print(f"     {LABEL[m]:16}{n:>8}{r:>8.1f}%{e:>+9.1f}{tag}")

    print(f"\n  3b. is the UNDER edge broad or one stat? (minutes-honest unders, per stat):")
    print(f"     {'stat':6}{'# bets':>8}{'hit %':>9}{'blind':>9}{'vs base':>10}")
    for s in STATS:
        d = un["MH"][s]
        r = 100 * d["w"] / d["n"] if d["n"] else float("nan")
        bu = 100 - base_over_s[s]
        print(f"     {s:6}{d['n']:>8}{r:>8.1f}%{bu:>8.1f}%{r - bu:>+9.1f}")

    print("\n  4. projection MAE (closeness, NOT betting — lower = better):")
    allmae = {m: st.mean([e for s in STATS for e in err[m][s]]) for m in METHODS}
    print("     " + "  ".join(f"{LABEL[m]} {allmae[m]:.2f}" for m in METHODS))
    print("\n  caveat: the line is set to the season average, i.e. a FULLY stale book. Real books")
    print("  price in some of the role change, so live hit rates run lower than this ceiling.")


if __name__ == "__main__":
    main()
