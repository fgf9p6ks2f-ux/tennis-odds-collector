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
import math
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

import rotowire as RW
import wnba_context as CTX
import wnba_dvp as DVP
import wnba_wowy as W

_RW_CACHE = {}


def rw_lineups():
    """RotoWire WNBA board (confirmed/projected lineups + ruled-out), fetched once per
    process and reused. Degrades to an empty board if RotoWire is unreachable, so the whole
    pipeline never hard-depends on it. Logs its status once so CI shows if it connected."""
    if "b" not in _RW_CACHE:
        try:
            _RW_CACHE["b"] = RW.board()
            print(f"RotoWire OK: {len(_RW_CACHE['b'])} lineups, "
                  f"{sum(len(t['out']) for t in _RW_CACHE['b'])} ruled out", flush=True)
        except Exception as e:
            _RW_CACHE["b"] = []
            print(f"RotoWire UNREACHABLE ({str(e)[:60]}) — ESPN fallback", flush=True)
    return _RW_CACHE["b"]

PROPS_DB = Path(os.environ.get("FD_DB",
                Path(__file__).resolve().parent / "fanduel_props.sqlite"))
# fd_lines stat keys we can project from a game log
PROP_STATS = {"points": "pts", "rebounds": "reb", "assists": "ast"}


def _am(dec):
    return f"+{round((dec-1)*100)}" if dec >= 2 else f"{round(-100/(dec-1))}"


def kelly_units(ph, n, dec, frac=0.25, unit_pct=0.04):
    """Recommended stake in UNITS via fractional (quarter) Kelly — sizes by edge AND odds,
    the math behind 'more on solid near-even bets, less on longshots'. p = the credibility-
    shrunk win prob (thin samples pulled toward the book); Kelly fraction f = (p·dec−1)/
    (dec−1); stake = frac·f of bankroll; 1u = unit_pct of bankroll. Quarter-Kelly + 1u=4%
    bankroll calibrates a solid ~-140/+100 edge to ~1u. Rounded to 0.5u, capped [0.5, 3]."""
    if ph is None or not n or dec <= 1:
        return 1.0
    p = (ph * n + (1.0 / dec) * 6) / (n + 6)          # shrink toward the book's implied prob
    f = (p * dec - 1) / (dec - 1)                     # full-Kelly fraction of bankroll
    if f <= 0:
        return 0.5
    return max(0.5, min(3.0, round((frac * f / unit_pct) * 2) / 2))


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
    """Latest WNBA props for a player: {stat_key: {line: (best_over_dec, best_under_dec)}}
    across books. Both sides now — the model bets whichever side the projection favors, so
    it needs the under price too (0.0 if a book only posted the over)."""
    if not PROPS_DB.exists():
        return {}
    con = sqlite3.connect(PROPS_DB)
    rows = con.execute(
        "SELECT stat, line, side, odds, COALESCE(book,'fd') FROM fd_lines "
        "WHERE sport='wnba' AND player=? AND collected_at > datetime('now','-1 day')",
        (player,)).fetchall()
    con.close()
    best = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # stat -> line -> [over, under]
    for stat, line, side, odds, _bk in rows:
        if stat in PROP_STATS and line is not None and side in ("over", "under"):
            k = round(float(line), 1)
            i = 0 if side == "over" else 1
            best[stat][k][i] = max(best[stat][k][i], float(odds))
    return {s: {k: tuple(v) for k, v in d.items()} for s, d in best.items()}


# Role floor scaled for WNBA's shorter 40-min game (NBA is 48): a bench player promoted
# to the starting lineup projects to ~22+ min, so judge production in their 22+ min games.
ROLE_FLOOR = 22.0

# Asymmetric EV bars from the backtest (14d+21d, leak-free) + the 07-09 real-line re-grade:
# UNDERS on reduced/regressing roles beat a blind baseline by +6 to +9 pts (reb/ast esp.);
# OVERS have ~no edge (elevated roles regress). So take the side minutes-honest favors, but
# demand much more edge to bet an over than an under.
OVER_EV_MIN = 0.10
UNDER_EV_MIN = 0.04
VOL_EV_MIN = 0.07     # volume-confirmed points OVERS EV bar
PRIMARY_FGA = 13.0    # baseline FGA at/above this = a primary option (Mabrey) — no room to grow, SKIP
# ROOM-TO-GROW volume model. The broad real-line backtest lost (-38%) because it flagged PRIMARY
# options (Mabrey: proj 24 -> scored 11, 0-4) whose shot count doesn't actually rise off an injury.
# Filtering to LOW-baseline-usage players (bench->starter + secondary starters like Hamby, who
# absorb the vacated shots) flips it: role players hit 54% on real lines (injury_volume_backtest,
# split by tier). So the volume over is live ONLY for room-to-grow players; the CLV shadow + ledger
# grade it forward, and the edge compounds with the injury-TIMING (bet before the line moves).
VOL_LIVE = True


# The user's per-stat decision model: which WOWY signals DECIDE each market.
#   points   -> FGA (shot volume/usage) + minutes   (scoring is opportunity-driven)
#   rebounds -> rebounds + minutes                   (own rate + playing time)
#   assists  -> assists + minutes                    (own rate + playing time)
# FGA is a POINTS driver only; it's noise for reb/ast.
STAT_DRIVER = {"points": "fga", "rebounds": "reb", "assists": "ast"}


_PG = {"G": "G", "PG": "G", "SG": "G", "GF": "G", "F": "F", "SF": "F", "PF": "F",
       "FC": "C", "C": "C", "CF": "C"}
_STAT_CTX = {"points": ("G", "F", "C"), "rebounds": ("C", "F"), "assists": ("G",)}


def _ctx_mean(sample, vals, stat, out_logs, mates):
    """Context-weighted mean of the (minutes-honest) sample: weight each game by how closely
    its lineup matched tonight — the out star(s) ABSENT (target 0 min) and the position-
    relevant competitors PRESENT (target = their expected minutes). Rebounds condition on the
    other bigs (C/F), assists on the guards. Kish-effective-n shrinkage toward the plain mean
    so a thin context match can't run wild; degrades to the plain mean when no context data."""
    ctx = [(bd, 0.0) for bd in (out_logs or [])]
    want = _STAT_CTX.get(stat, ("G", "F", "C"))
    ctx += [(bd, em) for (pg, bd, em) in (mates or []) if pg in want]
    if not ctx or len(vals) < 4:
        return st.mean(vals)
    ws = []
    for g in sample:
        d = g["date"][:10]
        wt = 1.0
        for bd, tgt in ctx:
            wt *= math.exp(-((bd.get(d, 0.0) - tgt) / 12.0) ** 2)
        ws.append(wt)
    wsum = sum(ws)
    if wsum <= 0:
        return st.mean(vals)
    wmean = sum(v * wt for v, wt in zip(vals, ws)) / wsum
    eff = wsum * wsum / sum(wt * wt for wt in ws)
    return (wmean * eff + st.mean(vals) * 5.0) / (eff + 5.0)


def project_all(log, proj_min):
    """Full minutes-honest projection (min + pts/reb/ast) for the projection TRACKER — mirrors
    prop_edges' elevated/breakout basis but for all three stats, regardless of flagging, so the
    background learner can grade every projection the model makes (not just the flagged bets)."""
    floor = max(proj_min - 4, ROLE_FLOOR)
    elevated = [g for g in log if g["min"] >= floor]
    if len(elevated) >= 4:
        sample, basis, cap = elevated, "elevated", 1.35
    else:
        sample = [g for g in log if g["min"] >= 12]
        basis, cap = "projected", 2.2
    if len(sample) < 3:
        return None

    def pj(key):
        return round(st.mean(g[key] * min(proj_min / max(g["min"], 1.0), cap) for g in sample), 2)

    return {"proj_min": round(proj_min, 1), "proj_pts": pj("pts"), "proj_reb": pj("reb"),
            "proj_ast": pj("ast"), "basis": basis, "n_games": len(sample)}


def _norm_sf(z):
    """P(X > z) for standard normal — no scipy."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def volume_points(log, proj_min, n_recent=4):
    """VOLUME-BASED points projection (the user's laddering edge). Shooting % is variance, but
    FGA/FTA volume is sticky (role/minutes-driven) — so project points off the ELEVATED volume at
    the player's SEASON efficiency (points per true-shot = pts / (FGA + 0.44·FTA)), not off recent
    points that carry single-game shooting noise the book fades. `confirmed` = the recent shot
    volume is genuinely elevated vs the pre-surge baseline (the real-role-vs-hot-night tell).
    Backtested (volume_points_backtest.py): more accurate than a recent-points projection in
    role-jump spots, and the over stays profitable where a recent-points over regresses."""
    games = sorted((g for g in log if (g.get("min") or 0) > 0), key=lambda g: g["date"])
    if len(games) < 5:
        return None
    ts_tot = sum(g["fga"] + 0.44 * g["fta"] for g in games)
    if ts_tot <= 0:
        return None
    pps = sum(g["pts"] for g in games) / ts_tot                 # season points per true-shot
    recent = games[-n_recent:]
    fga_p = st.mean(g["fga"] / max(g["min"], 1) for g in recent) * proj_min
    fta_p = st.mean(g["fta"] / max(g["min"], 1) for g in recent) * proj_min
    vol_pts = (fga_p + 0.44 * fta_p) * pps
    base = games[:-3] or games                                  # pre-surge baseline volume
    base_fga = st.mean(g["fga"] for g in base)
    recent_fga = st.mean(g["fga"] for g in games[-3:])
    sig = st.pstdev([g["pts"] for g in games[-8:]]) if len(games) >= 5 else 5.0
    # confirmed = volume genuinely rose AND the player has ROOM TO GROW (not already a primary
    # option who gets his shots regardless — those "jumps" are noise, backtested 0-4).
    return {"vol_pts": vol_pts,
            "confirmed": base_fga > 0 and recent_fga >= 1.35 * base_fga and base_fga < PRIMARY_FGA,
            "sigma": max(sig, 4.0), "pps": round(pps, 3), "base_fga": round(base_fga, 1),
            "recent_fga": round(recent_fga, 1), "fga_proj": round(fga_p, 1)}


def prop_edges(player, log, proj_min, w=None, vacated=None, ctx=None, out_logs=None, mates=None,
               opp=None, pos=None):
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
    if len(elevated) >= 4:
        sample, basis, shrink_k = elevated, "elevated", 6
        def val(g, key):
            # minutes-HONEST: scale each elevated game's production to TONIGHT's projected
            # minutes. Otherwise the model cherry-picks a player's 26-min games (Billings'
            # 7.4 reb) to project a ~19-min role. Capped at 1.35x so a genuine bump isn't
            # clipped; counting stats only (rate stats like FGA-per already normalize).
            r = min(proj_min / max(g["min"], 1.0), 1.35) if key in ("pts", "reb", "ast", "fga", "fta", "fg3a") else 1.0
            return g[key] * r
    else:
        # BREAKOUT fallback: thin elevated history but a real projected role. Project each
        # game to the projected minutes via per-minute rate (the user's method for a bench
        # player who just got the role — "she scores 16 and gets a big minutes boost").
        # Noisier, so tag it + shrink harder.
        base = [g for g in log if g["min"] >= 12]
        if len(base) < 3 or proj_min < ROLE_FLOOR:
            return []
        sample, basis, shrink_k = base, "projected", 9
        def val(g, key):
            return g[key] * min(proj_min / g["min"], 2.2)
    fga = st.mean([val(g, "fga") for g in sample])
    ctx = ctx or {}

    def wdelta(k):                                  # without-minus-with, or None if no split
        if not w or w.get("n_with", 0) < 1 or w.get("n_without", 0) < 1:
            return None
        return round(w["without"][k]["mean"] - w["with"][k]["mean"], 1)
    d_min, d_fga, d_fta, d_3pa = wdelta("min"), wdelta("fga"), wdelta("fta"), wdelta("fg3a")
    # VOLUME layer (points only): if the role's shot volume is genuinely elevated, project points
    # off that sticky volume and ladder the OVERS — the validated laddering edge.
    vp = volume_points(log, proj_min)
    vol_ok = bool(vp and vp["confirmed"])

    out = []
    for stat, best in posted_props(player).items():
        key = PROP_STATS[stat]
        season_avg = st.mean([g[key] for g in log]) if log else 0
        vals = [val(g, key) for g in sample]
        # plain minutes-honest mean. Context-weighting is dropped: the backtest showed it
        # diluted the under edge (MH-alone unders +8.4 vs +6.5 with context) for no MAE gain.
        elev_avg = st.mean(vals)
        # DvP tiebreaker: nudge toward the opponent's position- and pace-adjusted tendency to
        # allow this stat (small — dvp_backtest showed it's marginal, so it breaks ties/orders
        # overs by matchup but never overrides the validated under model). Logged as a feature.
        dvp_c = DVP.dvp(opp, pos, key) if (opp and pos) else 0.0
        elev_avg += dvp_c * proj_min
        use_vol = vol_ok and stat == "points" and VOL_LIVE     # VOL_LIVE=False: shadow only, no bets
        if use_vol:
            elev_avg = round(vp["vol_pts"], 1)      # sticky-volume projection drives the points ladder
        n = len(vals)
        # per-game samples for the bar chart: [value, opponent, minutes], most recent first
        recent = sorted(sample, key=lambda g: g["date"], reverse=True)[:10]
        # store the ACTUAL game stat (whole number) + minutes for the chart — not the minutes-
        # scaled projection value (which produced confusing decimals). The bars show what the
        # player really did each game vs the line; the minutes overlay shows the role context.
        samples = [[round(g[key]), g["matchup"], round(g["min"])] for g in recent]
        d_stat = wdelta(key)
        driver = wdelta(STAT_DRIVER[stat])          # the deciding signal for THIS market
        vac = round(vacated[stat], 1) if vacated and stat in vacated else None
        for line, (over_dec, under_dec) in sorted(best.items()):
            # Bet the side the minutes-honest projection favors vs THIS line: over if the
            # projection sits above the line, else under. The validated edge is the UNDER on
            # reduced/regressing roles; a strong over is still taken but must clear a higher bar.
            side = "over" if elev_avg >= line else "under"
            if use_vol and side == "under":           # the volume layer ladders OVERS only
                continue
            dec = over_dec if side == "over" else under_dec
            hi_odds = 7.0 if use_vol else 5.0          # volume overs LADDER UP to +600 alt lines
            if not dec or not (1.25 <= dec <= hi_odds):   # no price that side, or lottery longshot
                continue
            # skip trivial deep favorites — the line sits so far on one side of the projection
            # it's a near-lock with no edge (a 15-pt scorer's o3.5, or a u30.5), just clutter.
            if elev_avg > 0 and ((side == "over" and line < 0.4 * elev_avg)
                                 or (side == "under" and line > 2.5 * elev_avg)):
                continue
            # direction guard: don't bet an OVER on a stat that FALLS without the out player
            # (tolerate small negatives — early-season WOWY samples are thin/noisy).
            if side == "over" and d_stat is not None and d_stat < -1.0 and not use_vol:
                continue
            if use_vol:
                # P(points > line) from the VOLUME projection (normal around vol_pts) — strips the
                # single-game shooting variance the empirical elevated-game hit rate carries.
                hit = _norm_sf((line - vp["vol_pts"]) / vp["sigma"])
            else:
                hit = (sum(1 for v in vals if v > line) if side == "over"
                       else sum(1 for v in vals if v < line)) / n
            if hit >= 0.92 and dec >= 2.0:        # ~certain at plus money = mis-scrape, skip
                continue
            p_adj = (hit * n + (1 / dec) * shrink_k) / (n + shrink_k)
            ev = p_adj * dec - 1
            # stale line: the book anchored near the SEASON avg while the projected role sits
            # on the OTHER side of it — over above the season/proj midpoint, under below.
            mid = (season_avg + elev_avg) / 2
            stale = abs(elev_avg - season_avg) >= 1.0 and (
                (side == "over" and line <= mid) or (side == "under" and line >= mid))
            ev_bar = VOL_EV_MIN if use_vol else (OVER_EV_MIN if side == "over" else UNDER_EV_MIN)
            if ev >= ev_bar:
                spot = {"ev": ev, "stat": stat, "line": line, "dec": dec, "hit": hit,
                        "side": side,
                        "n": n, "fga": fga, "season_avg": round(season_avg, 1),
                        "elev_avg": round(elev_avg, 1), "stale": stale,
                        "d_stat": d_stat, "d_fga": d_fga, "d_min": d_min,
                        "driver": driver, "vac": vac,
                        # matchup environment (same across the team's props)
                        "total": ctx.get("total"), "pace": ctx.get("pace"),
                        "opp_def": ctx.get("opp_pts_allowed"),
                        # points-only scoring channels
                        "d_fta": d_fta if stat == "points" else None,
                        "d_3pa": d_3pa if stat == "points" else None,
                        # basis: 'volume' marks a volume-confirmed points-over ladder rung
                        "basis": "volume" if use_vol else basis, "samples": samples,
                        "vol": ({"vp": round(vp["vol_pts"], 1), "bf": vp["base_fga"],
                                 "rf": vp["recent_fga"], "pps": vp["pps"]} if use_vol else None)}
                out.append(spot)
    # collapse adjacent alt-line rungs (same stat, within 1.5 pts) to the best-value one —
    # e.g. keep points o10.5 over the redundant, more-juiced o9.5, but keep a real ladder
    # rung like o14.5 that's a distinct bet.
    out.sort(key=lambda d: -d["ev"])
    kept = []
    for e in out:
        if any(k["stat"] == e["stat"] and abs(k["line"] - e["line"]) <= 1.5 for k in kept):
            continue
        kept.append(e)
    return kept


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


def game_ids():
    """{team abbrev: ESPN game id} for today's (ET) slate — to look up the lineup."""
    et_date = dt.datetime.now(dt.timezone.utc).astimezone(ET).strftime("%Y%m%d")
    out = {}
    for e in _espn(f"scoreboard?dates={et_date}").get("events", []):
        gid = e.get("id")
        for c in e.get("competitions", [{}])[0].get("competitors", []):
            ab = c.get("team", {}).get("abbreviation")
            if ab and gid:
                out[ab] = gid
    return out


def game_starters(game_id):
    """{player_name: is_starter(bool)} once the lineup is SET (~30 min pre-tip / at tip),
    else None (lineup not posted yet). ESPN flags each box-score player as a starter."""
    if not game_id:
        return None
    out = {}
    for tm in _espn(f"summary?event={game_id}").get("boxscore", {}).get("players", []):
        for stt in tm.get("statistics", []):
            for a in stt.get("athletes", []):
                nm = a.get("athlete", {}).get("displayName")
                if nm and a.get("starter") is not None:
                    out[nm] = bool(a.get("starter"))
    return out or None                        # empty -> lineup not out yet


def starter_label(name, team, starters, proj_min):
    """Confidence in the elevated-MINUTES assumption (the user's key check — does the coach
    actually start the beneficiary). RotoWire is PRIMARY (confirmed/projected lineups posted
    hours ahead, locked ~30-60 min pre-tip); ESPN's box-score flag is the fallback:
       confirmed  — RotoWire (or ESPN) has them in a CONFIRMED starting five
       likely     — RotoWire has them in a PROJECTED five, or lineup TBD w/ starter-sized proj
       bench      — a lineup is posted for their team and they're NOT in it (proj too high)
       projected  — lineup TBD, a rotation bump (minutes less certain)"""
    board = rw_lineups()
    st = RW.starter_status(board, team, name) if (board and team) else None
    if st == "confirmed":
        return "confirmed"
    if st == "projected":
        return "likely"                       # in a projected lineup = likely to start
    if board and team and any(t["team"] == team.upper() for t in board):
        return "bench"                        # RotoWire posted this team's five, they're not in it
    if starters is not None:                  # ESPN fallback: box-score lineup is set
        return "confirmed" if starters.get(name) else "bench"
    return "likely" if proj_min >= 26 else "projected"


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
