"""WNBA WOWY (with-or-without-you) + minutes-band engine — PropsCash, automated.

The manual grind this replaces: when a key player sits, figure out who inherits the
minutes/usage, then recall how the beneficiary produced in past games at that role.
This computes it from the free WNBA stats API (stats.nba.com, LeagueID=10):

  wowy(player, teammate)  -> that player's MIN/PTS/REB/AST split by teammate IN vs OUT
  minutes_bands(player)   -> production distribution bucketed by minutes played
  beneficiaries(team, out)-> for a given out-list, who historically gains minutes/usage

Game-by-game: a game_id in player Y's log but NOT in teammate X's log = a game X missed,
so Y's rows there are the "without X" split. No injury feed needed for the history — the
absence IS the signal.

    python wnba_wowy.py --player "Jackie Young" --without "A'ja Wilson"
    python wnba_wowy.py --team LVA --out "A'ja Wilson"     # who benefits if Wilson sits
"""
from __future__ import annotations

import argparse
import math
import statistics as st
import time

import requests

# ESPN's public API — datacenter-reachable (stats.nba.com blocks cloud IPs, so we can't
# run that in CI). Rosters + per-game logs with the fields the usage model needs.
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_PLAYERS_CACHE = {}


def _get(url):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=H, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ESPN API failed: {url[:60]}")


def _made_att(s):
    """'8-14' -> (8, 14); robust to '--' / empty."""
    try:
        m, a = str(s).split("-")
        return int(m), int(a)
    except (ValueError, AttributeError):
        return 0, 0


def context_project(bene_log, contexts, statkey, scale=12.0, k=5.0):
    """Project a beneficiary's per-game `statkey` weighted toward evidence games whose LINEUP
    CONTEXT matches tonight — the missing piece the plain WOWY ignores.

    `contexts` = list of (teammate_game_log, expected_minutes_tonight). Each of the
    beneficiary's games is weighted by the PRODUCT of Gaussian kernels on how closely every
    context teammate's minutes that game match tonight's expectation. This captures, in ONE
    scheme, both:
      · the out star is absent  -> context (Clark_log, 0):   games where Clark sat weigh most
      · the other C is on court -> context (Boston_log, 35):  games where Billings was the 2nd
        big (few boards) weigh most, so a 12-rebound game where the C happened to be OUT is
        correctly discounted when the C starts tonight.
    Weighted mean shrunk toward the plain mean by k pseudo-obs (Kish effective-n), so a thin
    context-matched sample can't run wild. Returns (projection, effective_n)."""
    if not bene_log:
        return None, 0.0
    ctx = [({g["date"][:10]: g.get("min", 0.0) for g in log}, tgt) for log, tgt in contexts]
    xs, ws = [], []
    for g in bene_log:
        d = g["date"][:10]
        w = 1.0
        for by_date, tgt in ctx:
            cm = by_date.get(d, 0.0)                 # 0 = teammate didn't play that game
            w *= math.exp(-((cm - tgt) / scale) ** 2)
        xs.append(g.get(statkey, 0.0))
        ws.append(w)
    plain = sum(xs) / len(xs)
    wsum = sum(ws)
    if wsum <= 0:
        return plain, 0.0
    wmean = sum(x * w for x, w in zip(xs, ws)) / wsum
    eff = wsum * wsum / sum(w * w for w in ws)       # Kish effective sample size
    return (wmean * eff + plain * k) / (eff + k), eff


def game_log(pid):
    """[{game_id, date, min, pts, reb, ast, fga, fg3a, fta, tov, poss, dd, matchup}]
    for a player, from ESPN. fga/fta/tov feed the usage proxy; dd = double-double."""
    j = _get(f"{WEB}/athletes/{pid}/gamelog")
    labels = j.get("names") or []
    li = {name: k for k, name in enumerate(labels)}
    meta = j.get("events", {}) or {}
    out = []
    for stype in j.get("seasonTypes") or []:
        for cat in stype.get("categories") or []:
            for ev in cat.get("events") or []:
                s = ev.get("stats") or []
                eid = str(ev.get("eventId"))
                if len(s) < len(labels):
                    continue
                def num(key, d=0.0):
                    i = li.get(key)
                    try:
                        return float(s[i]) if i is not None else d
                    except (ValueError, TypeError):
                        return d
                p, rb, a = num("points"), num("totalRebounds"), num("assists")
                _fgm, fga = _made_att(s[li["fieldGoalsMade-fieldGoalsAttempted"]]) \
                    if "fieldGoalsMade-fieldGoalsAttempted" in li else (0, 0)
                _3m, fg3a = _made_att(s[li["threePointFieldGoalsMade-threePointFieldGoalsAttempted"]]) \
                    if "threePointFieldGoalsMade-threePointFieldGoalsAttempted" in li else (0, 0)
                _ftm, fta = _made_att(s[li["freeThrowsMade-freeThrowsAttempted"]]) \
                    if "freeThrowsMade-freeThrowsAttempted" in li else (0, 0)
                tov = num("turnovers")
                m = meta.get(eid, {})
                opp = (m.get("opponent") or {}).get("abbreviation", "")
                out.append({"game_id": eid, "date": m.get("gameDate", ""),
                            "min": num("minutes"), "pts": p, "reb": rb, "ast": a,
                            "fga": fga, "fg3a": fg3a, "fta": fta, "tov": tov,
                            "poss": fga + 0.44 * fta + tov,
                            "dd": sum(1 for v in (p, rb, a) if v >= 10) >= 2,
                            "matchup": opp,
                            "result": m.get("gameResult", "")})   # 'W'/'L' once FINAL, '' if not
    return out


def players():
    """{name: {'id','team','min','pts','reb','ast','gp','position'}} — rosters (ESPN) with
    season averages computed from each player's game log. Cached per process."""
    if _PLAYERS_CACHE:
        return _PLAYERS_CACHE
    teams = _get(f"{SITE}/teams").get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    out = {}
    for t in teams:
        tm = t["team"]
        abbr = tm["abbreviation"]
        roster = _get(f"{SITE}/teams/{tm['id']}/roster").get("athletes", [])
        for a in roster:
            pid, name = a.get("id"), a.get("displayName")
            pos = (a.get("position") or {}).get("abbreviation", "")
            if not pid or not name:
                continue
            try:
                log = game_log(pid)
            except RuntimeError:
                continue
            gp = len(log)
            if gp == 0:
                out[name] = {"id": pid, "team": abbr, "min": 0, "pts": 0, "reb": 0,
                             "ast": 0, "gp": 0, "position": pos}
                continue
            out[name] = {"id": pid, "team": abbr, "position": pos, "gp": gp,
                         "min": st.mean([g["min"] for g in log]),
                         "pts": st.mean([g["pts"] for g in log]),
                         "reb": st.mean([g["reb"] for g in log]),
                         "ast": st.mean([g["ast"] for g in log])}
            time.sleep(0.05)
    _PLAYERS_CACHE.update(out)
    return out


def _summ(games, stat):
    vals = [g[stat] for g in games]
    return {"n": len(vals), "mean": st.mean(vals) if vals else 0,
            "vals": sorted(vals, reverse=True)}


def wowy_multi(player_log, teammate_logs):
    """Split player's games by whether ALL the given teammates were ABSENT that game.
    'without' = games none of them played (the multi-out scenario the user cares about —
    a beneficiary often gets a BIGGER boost when 2+ impact players sit together); 'with' =
    at least one of them played. Returns per-stat means over MIN/PTS/REB/AST/FGA/FTA/3PA."""
    present = set()
    for tl in teammate_logs:
        present |= {g["game_id"] for g in tl}
    with_g = [g for g in player_log if g["game_id"] in present]
    without_g = [g for g in player_log if g["game_id"] not in present]
    def block(gs):
        return {s: _summ(gs, s) for s in ("min", "pts", "reb", "ast", "fga", "fta", "fg3a")}
    return {"with": block(with_g), "without": block(without_g),
            "n_with": len(with_g), "n_without": len(without_g)}


def wowy(player_log, teammate_log):
    """Single-teammate with/without split (thin wrapper over wowy_multi)."""
    return wowy_multi(player_log, [teammate_log])


def minutes_bands(pl_log, width=4):
    """Production bucketed by minutes played — the 'similar-minutes games' lookup."""
    bands = {}
    for g in pl_log:
        b = int(g["min"] // width) * width
        bands.setdefault(b, []).append(g)
    return {f"{b}-{b+width}": {s: _summ(gs, s) for s in ("pts", "reb", "ast")}
            for b, gs in sorted(bands.items())}


def _delta_line(label, w, wo):
    d = wo["mean"] - w["mean"]
    return (f"  {label:4} with {w['mean']:5.1f} (n{w['n']}) → without {wo['mean']:5.1f} "
            f"(n{wo['n']})   {d:+.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player")
    ap.add_argument("--without", help="teammate whose absence to split on")
    ap.add_argument("--team", help="show all players on a team")
    ap.add_argument("--out", help="with --team: teammate assumed out, rank beneficiaries")
    args = ap.parse_args()
    pl = players()

    if args.player and args.without:
        p, t = pl.get(args.player), pl.get(args.without)
        if not p or not t:
            raise SystemExit("player/teammate not found (exact name).")
        w = wowy(game_log(p["id"]), game_log(t["id"]))
        print(f"\n{args.player} — with vs WITHOUT {args.without}:")
        for s in ("min", "pts", "reb", "ast"):
            print(_delta_line(s.upper(), w["with"][s], w["without"][s]))
        print(f"\n{args.player} — production by minutes band:")
        for band, prod in minutes_bands(game_log(p["id"])).items():
            pts = prod["pts"]
            print(f"  {band:>7} min (n{pts['n']}): PTS {pts['vals']}")
        return

    if args.team and args.out:
        team_pl = {n: v for n, v in pl.items() if v["team"] == args.team.upper()}
        tout = pl.get(args.out)
        if not tout:
            raise SystemExit("out-player not found.")
        tlog = game_log(tout["id"])
        print(f"\nIf {args.out} sits — {args.team} beneficiaries (MIN & PTS gain WITHOUT him):")
        rows = []
        for n, v in team_pl.items():
            if n == args.out or v["gp"] < 5:
                continue
            try:
                w = wowy(game_log(v["id"]), tlog)
            except RuntimeError:
                continue
            if w["n_without"] >= 2:
                dmin = w["without"]["min"]["mean"] - w["with"]["min"]["mean"]
                dpts = w["without"]["pts"]["mean"] - w["with"]["pts"]["mean"]
                rows.append((dmin, dpts, n, w["n_without"]))
            time.sleep(0.2)
        for dmin, dpts, n, nw in sorted(rows, reverse=True)[:8]:
            print(f"  {n:22} {dmin:+.1f} min, {dpts:+.1f} pts   (n{nw} games without)")


if __name__ == "__main__":
    main()
