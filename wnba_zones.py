"""WNBA shot-location ZONES from ESPN play-by-play (free, CI-reachable) — the spatial scouting
layer. Basket at (x=25,y=0); dist=hypot(x-25,y) reproduces the play-text shot distances, so
shots classify into rim/paint/mid/long2/corner3/abovebreak3.

Delivers, from shooter=participants[0] and assister=participants[1] on made shots:
  · TEAM DEFENSE  — where each team gives up the most POINTS and ASSISTS by zone
  · PLAYER        — where each player SCORES and ASSISTS by zone
Rebound location is NOT available (the rebound coord is the missed-shot origin, not the
rebounder's spot; WNBA has no tracking data). Backtested (zone_backtest.py): zone matchup does
NOT improve betting projections on this short season — this is a SCOUTING tool, not a model input.

    python wnba_zones.py [days]                 # league + all-teams defensive zone profile
    python wnba_zones.py [days] --team ATL      # one team's points+assists allowed by zone
    python wnba_zones.py [days] --player "Clark" # one player's scoring+assist zones
    python wnba_zones.py [days] --md            # also write docs/wnba_scout.md
"""
import math
import sys
from collections import defaultdict
from pathlib import Path

import wnba_dvp as D
import wnba_pbp as P

HERE = Path(__file__).resolve().parent
ZONES = ("rim", "paint", "midrange", "long2", "corner3", "abovebreak3")


def zone(x, y, sv, text):
    if "three" in text.lower() or sv == 3:
        return "corner3" if y < 7.5 else "abovebreak3"
    d = math.hypot(x - 25, y)
    return "rim" if d < 4 else "paint" if d < 8 else "midrange" if d < 16 else "long2"


def parse_game(gid):
    j = P.fetch(gid)
    teams = {(tm.get("team") or {}).get("id"): (tm.get("team") or {}).get("abbreviation")
             for tm in j.get("boxscore", {}).get("teams", [])}
    ids = [i for i in teams if i]
    out = []
    if len(ids) != 2:
        return out
    for pl in j.get("plays", []):
        if not pl.get("shootingPlay"):
            continue
        c = pl.get("coordinate", {}) or {}
        x, y = c.get("x", -2e9), c.get("y", -2e9)
        text = pl.get("text", "") or ""
        if x < -1e9 or "free throw" in text.lower():
            continue
        stid = (pl.get("team") or {}).get("id")
        if stid not in teams:
            continue
        parts = pl.get("participants") or []
        shooter = text.split(" makes ")[0].split(" misses ")[0].strip()
        made = bool(pl.get("scoringPlay"))
        assister = ""
        if made and "assists)" in text and len(parts) >= 2:
            assister = text.split("(")[-1].split(" assists")[0].strip()
        out.append({"shooter": shooter, "assister": assister,
                    "off": teams[stid], "def": teams[ids[0] if ids[1] == stid else ids[1]],
                    "zone": zone(x, y, pl.get("scoreValue"), text),
                    "made": made, "pts": pl.get("scoreValue", 0) if made else 0})
    return out


def collect(days):
    shots = []
    gids = D._game_ids(days)
    _, wnba = D.positions()
    for gid in gids:
        try:
            shots += [s for s in parse_game(gid) if s["off"] in wnba and s["def"] in wnba]
        except Exception:
            continue
    return shots, len(gids)


def _row(vals, w=8):
    return "".join(f"{v:>{w}}" for v in vals)


def team_defense(shots):
    """{team: {zone: {'pts_allowed', 'ast_allowed', 'fga', 'fgm', 'games'}}}"""
    gp = defaultdict(set)
    d = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for s in shots:
        z = d[s["def"]][s["zone"]]
        z["fga"] += 1
        z["fgm"] += s["made"]
        z["pts_allowed"] += s["pts"]
        z["ast_allowed"] += 1 if (s["made"] and s["assister"]) else 0
    return d


def main():
    args = sys.argv[1:]
    days = next((int(a) for a in args if a.isdigit()), 14)
    team = args[args.index("--team") + 1] if "--team" in args else None
    player = args[args.index("--player") + 1] if "--player" in args else None
    shots, ng = collect(days)
    lines = [f"# WNBA zone scouting — last {days} days ({ng} games, {len(shots)} located shots)", ""]

    lg = defaultdict(lambda: defaultdict(float))
    for s in shots:
        lg[s["zone"]]["fga"] += 1
        lg[s["zone"]]["fgm"] += s["made"]
        lg[s["zone"]]["pts"] += s["pts"]
    tot = sum(lg[z]["fga"] for z in ZONES) or 1
    lines += ["## League by zone", "```",
              _row(["zone", "att%", "FG%", "pts/shot"], 12)]
    for z in ZONES:
        v = lg[z]
        fg = f"{100*v['fgm']/v['fga']:.0f}%" if v["fga"] else "-"
        lines.append(_row([z, f"{100*v['fga']/tot:.0f}%", fg, f"{v['pts']/v['fga']:.2f}"], 12))
    lines += ["```", ""]

    dd = team_defense(shots)
    # per-zone SHARES of a team's allowed points/assists (game-count-independent) + FG% allowed
    if team:
        teams = [team.upper()]
    else:
        teams = sorted(dd.keys())
    lines += ["## Team DEFENSE — where each team gives up the most (per zone)", ""]
    for t in teams:
        zt = dd.get(t, {})
        tot_pts = sum(zt[z]["pts_allowed"] for z in ZONES) or 1
        tot_ast = sum(zt[z]["ast_allowed"] for z in ZONES) or 1
        lines += [f"### {t}", "```", _row(["zone", "pts%", "ast%", "FG%allow", "vs lgFG"], 12)]
        for z in ZONES:
            v = zt.get(z, {"pts_allowed": 0, "ast_allowed": 0, "fga": 0, "fgm": 0})
            fga, fgm = v["fga"], v["fgm"]
            af = 100 * fgm / fga if fga else 0
            lf = 100 * lg[z]["fgm"] / lg[z]["fga"] if lg[z]["fga"] else 0
            edge = f"{af-lf:+.0f}"
            lines.append(_row([z, f"{100*v['pts_allowed']/tot_pts:.0f}%",
                               f"{100*v['ast_allowed']/tot_ast:.0f}%", f"{af:.0f}%", edge], 12))
        # headline: the softest zone by points share above league share
        soft = max(ZONES, key=lambda z: (zt.get(z, {}).get("pts_allowed", 0) / tot_pts)
                   - (lg[z]["pts"] / (sum(lg[zz]["pts"] for zz in ZONES) or 1)))
        lines += ["```", f"-> gives up the most (vs league): **{soft}**", ""]

    if player:
        ps = defaultdict(lambda: defaultdict(float))
        pa = defaultdict(float)
        for s in shots:
            if player.lower() in s["shooter"].lower():
                ps[s["zone"]]["fga"] += 1
                ps[s["zone"]]["fgm"] += s["made"]
                ps[s["zone"]]["pts"] += s["pts"]
            if s["assister"] and player.lower() in s["assister"].lower():
                pa[s["zone"]] += 1
        lines += [f"## {player} — scoring & assists by zone", "```",
                  _row(["zone", "FGA", "FGM", "FG%", "pts", "assists"], 10)]
        for z in ZONES:
            v = ps.get(z)
            if v and v["fga"]:
                fg = f"{100*v['fgm']/v['fga']:.0f}%"
                lines.append(_row([z, int(v["fga"]), int(v["fgm"]), fg, int(v["pts"]),
                                   int(pa.get(z, 0))], 10))
        lines += ["```", ""]

    out = "\n".join(lines)
    print(out if (team or player) else "\n".join(lines[:40]) + "\n... (--md for full)")
    if "--md" in args:
        (HERE / "docs").mkdir(exist_ok=True)
        (HERE / "docs" / "wnba_scout.md").write_text(out + "\n")
        print("\nwrote docs/wnba_scout.md")


if __name__ == "__main__":
    main()
