"""WNBA tonight board — the TRIGGER. Turns 'who's out' into 'here are the spots'.

Ties tonight's ESPN injury report + schedule to the WOWY engine: for every key player
ruled OUT on a team playing tonight, surface who inherits the minutes/usage and their
production in past games at that role — so the spot finds YOU instead of you memorizing
lineups. This is step 1 of 3 (trigger -> prop-line integration -> DvP).

    python wnba_tonight.py             # tonight's absences + beneficiaries
    python wnba_tonight.py --min-out 22  # only key players (>=22 mpg) being out
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = dt.timezone(dt.timedelta(hours=-4))

import wnba_context as CTX
import wnba_wowy as W

PROPS_DB = Path(os.environ.get("FD_DB",
                Path(__file__).resolve().parent / "fanduel_props.sqlite"))
# fd_lines stat keys we can project from a game log
PROP_STATS = {"points": "pts", "rebounds": "reb", "assists": "ast"}


def _am(dec):
    return f"+{round((dec-1)*100)}" if dec >= 2 else f"{round(-100/(dec-1))}"


def playing_now(player):
    """True if a supposedly-out player actually has FRESH props posted — books pull a
    player's props the moment they're ruled out, so a full slate in the latest collection
    cycle means they're PLAYING. Guards against a stale injury feed (e.g. a returning
    player still tagged 'Out', like A'ja Wilson 7/9)."""
    if not PROPS_DB.exists():
        return False
    con = sqlite3.connect(PROPS_DB)
    latest = con.execute("SELECT MAX(collected_at) FROM fd_lines WHERE sport='wnba'").fetchone()[0]
    if not latest:
        con.close()
        return False
    n = con.execute("SELECT COUNT(*) FROM fd_lines WHERE sport='wnba' AND player=? "
                    "AND collected_at >= datetime(?, '-45 minutes')", (player, latest)).fetchone()[0]
    con.close()
    return n > 0


def posted_props(player):
    """Latest WNBA props for a player: {stat_key: {line: best_over_dec}} across books."""
    if not PROPS_DB.exists():
        return {}
    con = sqlite3.connect(PROPS_DB)
    rows = con.execute(
        "SELECT stat, line, side, odds, COALESCE(book,'fd') FROM fd_lines "
        "WHERE sport='wnba' AND player=? AND collected_at > datetime('now','-1 day')",
        (player,)).fetchall()
    con.close()
    best = defaultdict(dict)
    for stat, line, side, odds, _bk in rows:
        if stat in PROP_STATS and side == "over" and line is not None:
            k = round(float(line), 1)
            best[stat][k] = max(best[stat].get(k, 0), float(odds))
    return best


# Role floor scaled for WNBA's shorter 40-min game (NBA is 48): a bench player promoted
# to the starting lineup projects to ~22+ min, so judge production in their 22+ min games.
ROLE_FLOOR = 22.0


# The user's per-stat decision model: which WOWY signals DECIDE each market.
#   points   -> FGA (shot volume/usage) + minutes   (scoring is opportunity-driven)
#   rebounds -> rebounds + minutes                   (own rate + playing time)
#   assists  -> assists + minutes                    (own rate + playing time)
# FGA is a POINTS driver only; it's noise for reb/ast.
STAT_DRIVER = {"points": "fga", "rebounds": "reb", "assists": "ast"}


def prop_edges(player, log, proj_min, w=None, vacated=None, ctx=None):
    """+EV over-props, framed as the user's actual edge: the gap between ELEVATED-ROLE
    production and a line the book anchored to the SEASON AVERAGE. For each posted line:
    hit rate in the player's elevated games (min >= max(proj-4, 22)), credibility-shrunk
    to the book's implied prob (thin samples + the book set the line), flagged when +EV.

    Per-stat judgment signals (`w` = beneficiary's WOWY split vs the OUT player):
      points   decided on d_fga + d_min;  rebounds on d_reb + d_min;  assists on d_ast + d_min.
    Points also carry d_fta/d_3pa (line/three scoring channels). `vacated` = the out
    player's own avg in that stat = SIZE of the redistributed pool. `ctx` = matchup
    context (Vegas total, pace, opp points allowed) — the game environment, which lifts
    all counting stats. All attached as features; the learned model weights them. Won't
    post an over on a stat that DROPS without the player. Returns list of dicts."""
    floor = max(proj_min - 4, ROLE_FLOOR)
    elevated = [g for g in log if g["min"] >= floor]
    if len(elevated) < 4:
        return []
    fga = st.mean([g["fga"] for g in elevated])
    ctx = ctx or {}

    def wdelta(k):                                  # without-minus-with, or None if no split
        if not w or w.get("n_with", 0) < 1 or w.get("n_without", 0) < 1:
            return None
        return round(w["without"][k]["mean"] - w["with"][k]["mean"], 1)
    d_min, d_fga, d_fta, d_3pa = wdelta("min"), wdelta("fga"), wdelta("fta"), wdelta("fg3a")

    out = []
    for stat, best in posted_props(player).items():
        key = PROP_STATS[stat]
        season_avg = st.mean([g[key] for g in log]) if log else 0
        vals = [g[key] for g in elevated]
        elev_avg = st.mean(vals)
        n = len(vals)
        d_stat = wdelta(key)
        driver = wdelta(STAT_DRIVER[stat])          # the deciding signal for THIS market
        vac = round(vacated[stat], 1) if vacated and stat in vacated else None
        # the user bets the INCREASE — don't post an over on a stat that falls without the
        # out player (tolerate small negatives: WOWY samples are thin/noisy early season).
        if d_stat is not None and d_stat < -1.0:
            continue
        for line, dec in sorted(best.items()):
            # Keep the credible market, but don't throw out legit LADDER bets (the user
            # plays o9.5 up to 15+ on a big projection). Fake-EV artifacts are specifically
            # a NEAR-LOCK PRICED AS AN UNDERDOG (a 20-pt scorer's o4.5 @ +830) — filter THAT,
            # not fairly-priced alt lines.
            if line < 0.4 * elev_avg:            # only an absurd deep rung (o4.5 for a 16-projector)
                continue
            if not (1.25 <= dec <= 5.0):          # allow +400 ladders; cap only lottery longshots
                continue
            hit = sum(1 for v in vals if v > line) / n
            if hit >= 0.92 and dec >= 2.0:        # ~certain over at plus money = mis-scrape, skip
                continue
            p_adj = (hit * n + (1 / dec) * 6) / (n + 6)
            ev = p_adj * dec - 1
            # the user's edge: line anchored near the SEASON avg while the elevated role
            # projects meaningfully higher — the book hasn't repriced the new role.
            stale = elev_avg - season_avg >= 1.0 and line <= (season_avg + elev_avg) / 2
            if ev >= 0.05:
                spot = {"ev": ev, "stat": stat, "line": line, "dec": dec, "hit": hit,
                        "n": n, "fga": fga, "season_avg": round(season_avg, 1),
                        "elev_avg": round(elev_avg, 1), "stale": stale,
                        "d_stat": d_stat, "d_fga": d_fga, "d_min": d_min,
                        "driver": driver, "vac": vac,
                        # matchup environment (same across the team's props)
                        "total": ctx.get("total"), "pace": ctx.get("pace"),
                        "opp_def": ctx.get("opp_pts_allowed"),
                        # points-only scoring channels
                        "d_fta": d_fta if stat == "points" else None,
                        "d_3pa": d_3pa if stat == "points" else None}
                out.append(spot)
    return sorted(out, key=lambda d: -d["ev"])


def double_double_rate(log, proj_min, w=None):
    """DD hit rate in the player's elevated-role games — the lagging high-odds market on
    backup bigs (Embiid out → Drummond DD at 2.5-4x). Threads the same judgment signals:
    the reb/pts/min RISE without the out player (`w`), the two stats a big's DD is built
    from. Returns {rate, n, d_reb, d_pts, d_min} or None if thin / role clearly shrinks."""
    floor = max(proj_min - 4, ROLE_FLOOR - 5)
    elevated = [g for g in log if g["min"] >= floor]
    if len(elevated) < 4:
        return None

    def wd(k):
        if not w or w.get("n_with", 0) < 1 or w.get("n_without", 0) < 1:
            return None
        return round(w["without"][k]["mean"] - w["with"][k]["mean"], 1)
    d_reb, d_pts, d_min = wd("reb"), wd("pts"), wd("min")
    # the DD comes from the role EXPANDING — skip if both scoring and boards fall off
    if d_reb is not None and d_pts is not None and d_reb < -1.0 and d_pts < -1.0:
        return None
    return {"rate": sum(1 for g in elevated if g["dd"]) / len(elevated), "n": len(elevated),
            "d_reb": d_reb, "d_pts": d_pts, "d_min": d_min}

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
EH = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
# players() and the scoreboard are both ESPN now — abbrevs already match, no remap.
TEAM_FIX = {}


def _espn(path):
    r = requests.get(f"{ESPN}/{path}", headers=EH, timeout=20)
    return r.json() if r.status_code == 200 else {}


def tonight_matchups():
    """{team abbrev: opponent abbrev} for TODAY's (US Eastern) non-final games. Query the
    explicit ET date, NOT ESPN's default /scoreboard — the default stays stuck on
    yesterday's finished slate until late morning ET, so the early crons would see zero
    games. All four crons (18/21/23 + 00:30 UTC) map to the same ET slate date."""
    et_date = dt.datetime.now(dt.timezone.utc).astimezone(ET).strftime("%Y%m%d")
    out = {}
    for e in _espn(f"scoreboard?dates={et_date}").get("events", []):
        if e.get("status", {}).get("type", {}).get("state") == "post":
            continue
        comp = e.get("competitions", [{}])[0].get("competitors", [])
        abs_ = [TEAM_FIX.get(c.get("team", {}).get("abbreviation", ""),
                             c.get("team", {}).get("abbreviation", "")) for c in comp]
        if len(abs_) == 2:
            out[abs_[0]] = abs_[1]
            out[abs_[1]] = abs_[0]
    return out


def tonight_teams():
    return set(tonight_matchups())


def injuries():
    """{player_name: status} for Out / Doubtful / Questionable."""
    out = {}
    for t in _espn("injuries").get("injuries", []):
        for p in t.get("injuries") or []:
            nm = p.get("athlete", {}).get("displayName")
            status = p.get("status")
            if nm and status in ("Out", "Doubtful", "Questionable"):
                out[nm] = status
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-out", type=float, default=20.0,
                    help="only flag absences of players averaging >= this many minutes")
    args = ap.parse_args()

    pl = W.players()
    matchups = tonight_matchups()
    playing = set(matchups)
    inj = injuries()
    # truly out = listed Out/Doubtful AND no fresh posted props (books pull props for the
    # genuinely out; a returning player still tagged 'Out' still has a full slate)
    out_names = {n for n, s in inj.items() if s in ("Out", "Doubtful") and not playing_now(n)}
    lines, rates = CTX.game_lines(), CTX.team_rates()     # Vegas total + pace, once
    print(f"Tonight: {len(playing)} teams in action · {len(inj)} injury-listed players\n")

    # key OUT players whose team plays tonight
    flagged = []
    for name, status in inj.items():
        p = pl.get(name)
        if not p or p["team"] not in playing or p["min"] < args.min_out:
            continue
        if status not in ("Out", "Doubtful") or playing_now(name):   # skip stale 'Out'
            continue
        flagged.append((name, status, p))
    flagged.sort(key=lambda x: -x[2]["min"])

    if not flagged:
        print("no key players ruled out on tonight's slate yet — check back ~30min pre-tip.")
        return

    for name, status, p in flagged:
        opp = matchups.get(p["team"], "")
        note = CTX.matchup_note(p["team"], opp, lines, rates)
        ctx = CTX.matchup_context(p["team"], opp, lines, rates)
        print(f"=== {name} ({p['team']}) {status} — {p['min']:.0f} mpg, {p['pts']:.0f} ppg "
              f"vacated ===" + (f"  [{note}]" if note else ""))
        try:
            tlog = W.game_log(p["id"])
            team_pl = {n: v for n, v in pl.items()
                       if v["team"] == p["team"] and n != name and v["gp"] >= 5
                       and n not in out_names}
            rows = []
            for n, v in team_pl.items():
                blog = W.game_log(v["id"])
                w = W.wowy(blog, tlog)
                if w["n_without"] >= 2:
                    dmin = w["without"]["min"]["mean"] - w["with"]["min"]["mean"]
                    dpts = w["without"]["pts"]["mean"] - w["with"]["pts"]["mean"]
                    dfga = w["without"]["fga"]["mean"] - w["with"]["fga"]["mean"]
                    rows.append((dmin, dpts, dfga, n, w, blog))
            vacated = {"points": p["pts"], "rebounds": p["reb"], "assists": p["ast"]}
            for dmin, dpts, dfga, n, w, blog in sorted(rows, key=lambda r: (-r[0], -r[1]))[:4]:
                proj_min = w["without"]["min"]["mean"]
                # the user's judgment, on one line: more minutes, more shots, more production
                print(f"  {n:22} → ~{proj_min:.0f}min ({dmin:+.0f}), {dpts:+.1f}pts, "
                      f"{dfga:+.1f}FGA w/o {name.split()[-1]}")
                for e in prop_edges(n, blog, proj_min, w, vacated, ctx):
                    star = " ⟵ stale line" if e["stale"] else ""
                    dl = {"points": "FGA", "rebounds": "reb", "assists": "ast"}[e["stat"]]
                    d = f"{dl} {e['driver']:+g}, min {e['d_min']:+g} w/o, " if e["driver"] is not None else ""
                    ch = (f" [FTA {e['d_fta']:+g}, 3PA {e['d_3pa']:+g}]"
                          if e["stat"] == "points" and e["d_fta"] is not None else "")
                    print(f"       ✅ {e['stat']} over {e['line']:g} @ {_am(e['dec'])} — "
                          f"{d}elev {e['elev_avg']:g} vs season {e['season_avg']:g}, "
                          f"hit {e['hit']*100:.0f}%/{e['n']}g (+{e['ev']*100:.0f}% EV){star}{ch}")
                dd = double_double_rate(blog, proj_min, w)
                if dd and dd["rate"] >= 0.35:            # check the lagging DD market
                    wo = ""
                    if dd["d_reb"] is not None:
                        wo = f" (reb {dd['d_reb']:+g}, pts {dd['d_pts']:+g} w/o)"
                    print(f"       ★ double-double {dd['rate']*100:.0f}% in {dd['n']} elevated "
                          f"games{wo} — check the DD price (often stale for backup bigs)")
        except RuntimeError:
            print("  (stats fetch failed, retry)")
        print()


if __name__ == "__main__":
    main()
