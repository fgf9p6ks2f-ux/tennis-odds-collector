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
import statistics as st
import time

import requests

API = "https://stats.nba.com/stats"
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
     "Referer": "https://www.wnba.com/", "Origin": "https://www.wnba.com",
     "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
     "Accept": "application/json"}
SEASON = "2026"


def _get(endpoint, **params):
    params.setdefault("LeagueID", "10")
    for attempt in range(3):
        try:
            r = requests.get(f"{API}/{endpoint}", params=params, headers=H, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"WNBA API failed: {endpoint}")


def _table(j, i=0):
    rs = j["resultSets"][i]
    idx = {h: k for k, h in enumerate(rs["headers"])}
    return idx, rs["rowSet"]


def players():
    """{name: {'id','team','min','pts','reb','ast','usage_proxy','gp'}} season averages."""
    j = _get("leaguedashplayerstats", Season=SEASON, SeasonType="Regular Season",
             PerMode="PerGame", MeasureType="Base", PORound="0", Month="0", Period="0",
             LastNGames="0", TeamID="0", OpponentTeamID="0", Outcome="", Location="",
             SeasonSegment="", DateFrom="", DateTo="", VsConference="", VsDivision="",
             GameSegment="", Conference="", Division="", GameScope="", PlayerExperience="",
             PlayerPosition="", StarterBench="", TwoWay="0")
    idx, rows = _table(j)
    out = {}
    for r in rows:
        out[r[idx["PLAYER_NAME"]]] = {
            "id": r[idx["PLAYER_ID"]], "team": r[idx["TEAM_ABBREVIATION"]],
            "min": r[idx["MIN"]], "pts": r[idx["PTS"]], "reb": r[idx["REB"]],
            "ast": r[idx["AST"]], "gp": r[idx["GP"]]}
    return out


def game_log(pid):
    """[{game_id, date, min, pts, reb, ast, fga, fg3a, dd, matchup}] for a player.
    fga/fg3a = shot volume (the usage tell the user reads); dd = double-double (10+ in
    two of pts/reb/ast — the lagging derivative market on backup bigs)."""
    j = _get("playergamelog", PlayerID=pid, Season=SEASON, SeasonType="Regular Season")
    idx, rows = _table(j)
    out = []
    for r in rows:
        p, rb, a = r[idx["PTS"]], r[idx["REB"]], r[idx["AST"]]
        fga, fta, tov = r[idx["FGA"]], r[idx["FTA"]], r[idx["TOV"]]
        dd = sum(1 for v in (p, rb, a) if v >= 10) >= 2
        out.append({"game_id": r[idx["Game_ID"]], "date": r[idx["GAME_DATE"]],
                    "min": r[idx["MIN"]], "pts": p, "reb": rb, "ast": a,
                    "fga": fga, "fg3a": r[idx["FG3A"]], "fta": fta, "tov": tov,
                    "poss": fga + 0.44 * fta + tov,     # usage proxy: possessions used
                    "dd": dd, "matchup": r[idx["MATCHUP"]]})
    return out


def _summ(games, stat):
    vals = [g[stat] for g in games]
    return {"n": len(vals), "mean": st.mean(vals) if vals else 0,
            "vals": sorted(vals, reverse=True)}


def wowy(player_log, teammate_log):
    """Split player's games by whether the teammate PLAYED that game. Returns
    {'with':..., 'without':...} each with per-stat mean over MIN/PTS/REB/AST."""
    tm_games = {g["game_id"] for g in teammate_log}
    with_g = [g for g in player_log if g["game_id"] in tm_games]
    without_g = [g for g in player_log if g["game_id"] not in tm_games]
    def block(gs):
        return {s: _summ(gs, s) for s in ("min", "pts", "reb", "ast")}
    return {"with": block(with_g), "without": block(without_g),
            "n_with": len(with_g), "n_without": len(without_g)}


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
