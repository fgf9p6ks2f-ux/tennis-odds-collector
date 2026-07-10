"""WNBA shot-location ZONES from ESPN play-by-play (free, CI-reachable) — the spatial layer.

ESPN PBP gives every shot a coordinate; the basket sits at (x=25, y=0), so
dist = hypot(x-25, y) reproduces the shot distances in the play text. From that:
  · PLAYER shot profile  — where each player attempts + makes shots (rim/paint/mid/long2/3s)
  · TEAM zone DEFENSE    — where each team allows the most attempts / makes / points
Shooter = participants[0]; assister = participants[1] on made shots (so assists-by-zone is
also available). Rebound coordinates are the MISSED-SHOT origin, not the rebounder's spot, so
physical rebound-location is NOT available (WNBA has no tracking data).

    python wnba_zones.py [days] [--team ATL] [--player "Caitlin Clark"]
"""
import math
import sys
from collections import defaultdict

import wnba_dvp as D          # _game_ids reuse
import wnba_pbp as P

ZONES = ("rim", "paint", "midrange", "long2", "corner3", "abovebreak3")


def zone(x, y, sv, text):
    if "three" in text.lower() or sv == 3:
        return "corner3" if y < 7.5 else "abovebreak3"
    dist = math.hypot(x - 25, y)
    if dist < 4:
        return "rim"
    if dist < 8:
        return "paint"
    if dist < 16:
        return "midrange"
    return "long2"


def parse_game(gid):
    j = P.fetch(gid)
    teams = {(tm.get("team") or {}).get("id"): (tm.get("team") or {}).get("abbreviation")
             for tm in j.get("boxscore", {}).get("teams", [])}
    ids = [i for i in teams if i]
    out = []
    for pl in j.get("plays", []):
        if not pl.get("shootingPlay"):
            continue
        c = pl.get("coordinate", {}) or {}
        x, y = c.get("x", -2e9), c.get("y", -2e9)
        if x < -1e9:
            continue                                   # free throw / no location
        text = pl.get("text", "")
        if "free throw" in text.lower():
            continue
        stid = (pl.get("team") or {}).get("id")
        if stid not in teams or len(ids) != 2:
            continue
        parts = pl.get("participants") or []
        name = text.split(" makes ")[0].split(" misses ")[0].strip() if text else ""
        made = bool(pl.get("scoringPlay"))
        out.append({"name": name, "off": teams[stid],
                    "def": teams[ids[0] if ids[1] == stid else ids[1]],
                    "zone": zone(x, y, pl.get("scoreValue"), text),
                    "made": made, "pts": pl.get("scoreValue", 0) if made else 0})
    return out


def collect(days):
    shots = []
    gids = D._game_ids(days)
    for gid in gids:
        try:
            shots += parse_game(gid)
        except Exception:
            continue
    return shots, len(gids)


def _pct(made, att):
    return f"{100*made/att:.0f}%" if att else "  -"


def main():
    args = sys.argv[1:]
    days = next((int(a) for a in args if a.isdigit()), 14)
    team = next((args[args.index("--team") + 1] for a in args if a == "--team"), None)
    player = next((args[args.index("--player") + 1] for a in args if a == "--player"), None)
    shots, ng = collect(days)
    print(f"\nWNBA ZONES — last {days} days, {ng} games, {len(shots)} located shots\n")

    # league baseline by zone
    lg = defaultdict(lambda: [0, 0, 0])              # zone -> [att, made, pts]
    for s in shots:
        z = lg[s["zone"]]
        z[0] += 1
        z[1] += s["made"]
        z[2] += s["pts"]
    tot = sum(v[0] for v in lg.values()) or 1
    print("  LEAGUE shot distribution / efficiency by zone:")
    print(f"    {'zone':12}{'att%':>7}{'FG%':>7}{'pts/shot':>10}")
    for z in ZONES:
        a, m, p = lg[z]
        print(f"    {z:12}{100*a/tot:>6.0f}%{_pct(m,a):>7}{(p/a if a else 0):>10.2f}")

    if team:
        dz = defaultdict(lambda: [0, 0, 0])
        for s in shots:
            if s["def"] == team.upper():
                z = dz[s["zone"]]
                z[0] += 1
                z[1] += s["made"]
                z[2] += s["pts"]
        gp = len({0}) or 1
        print(f"\n  {team.upper()} DEFENSE — shots ALLOWED by zone (vs league FG%):")
        print(f"    {'zone':12}{'allowed FG%':>13}{'lg FG%':>9}{'  edge':>8}")
        for z in ZONES:
            a, m, _ = dz[z]
            la, lm, _ = lg[z]
            af = 100 * m / a if a else 0
            lf = 100 * lm / la if la else 0
            tag = " SOFT" if af - lf > 3 else (" tough" if af - lf < -3 else "")
            print(f"    {z:12}{_pct(m,a):>13}{_pct(lm,la):>9}{af-lf:>+7.0f}{tag}")

    if player:
        pz = defaultdict(lambda: [0, 0, 0])
        for s in shots:
            if player.lower() in s["name"].lower():
                z = pz[s["zone"]]
                z[0] += 1
                z[1] += s["made"]
                z[2] += s["pts"]
        tt = sum(v[0] for v in pz.values())
        print(f"\n  {player} — shot profile by zone ({tt} shots):")
        print(f"    {'zone':12}{'att':>5}{'made':>6}{'FG%':>7}{'pts':>6}")
        for z in ZONES:
            a, m, p = pz[z]
            if a:
                print(f"    {z:12}{a:>5}{m:>6}{_pct(m,a):>7}{p:>6}")


if __name__ == "__main__":
    main()
