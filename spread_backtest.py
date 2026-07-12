"""Do injury-driven OVERS underperform when the beneficiary's team gets blown out?

The user's hypothesis: a big underdog gets blown out -> starters benched in garbage time / cold /
demoralized -> overs miss. The actionable pre-game signal is the SPREAD, but ESPN drops pre-game
odds once a game is final, so there are no historical spreads to backtest. The next-best proxy is
the actual final MARGIN (a blowout is what a big spread predicts). Caveat: margin is post-hoc and
weakly circular (a beneficiary's own under-production nudges the margin) — but the beneficiary is
1 of 5+ players, so the margin is mostly exogenous to their single line. Treat as directional.

Leak-free spots: 2+ impact players (>=18 mpg) out for a team-game, each beneficiary who played,
bet the OVER at their stale normal-role baseline (mean with the out-set PLAYING). Split the over
hit-rate by the beneficiary's team margin that game.

    python spread_backtest.py
"""
import json
import ssl
import statistics as st
import urllib.request
from collections import defaultdict

import wnba_wowy as W

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
IMPACT = 18.0
MIN_NWO = 3


def _scoreboard(dates):
    """{(date10, team_abbr): margin} — team score minus opponent score, from ESPN finals."""
    margin = {}
    for d in sorted(dates):
        url = ("https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates="
               + d.replace("-", ""))
        try:
            j = json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                timeout=20, context=_CTX))
        except Exception:
            continue
        for e in j.get("events", []):
            comp = (e.get("competitions") or [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) != 2:
                continue
            try:
                sc = {c["team"]["abbreviation"]: int(c["score"]) for c in cs}
            except (KeyError, ValueError, TypeError):
                continue
            (a, sa), (b, sb) = sc.items()
            margin[(d, a)] = sa - sb
            margin[(d, b)] = sb - sa
    return margin


def main():
    pl = W.players()
    logs, byteam = {}, defaultdict(list)
    for n, v in pl.items():
        if v["gp"] < 5:
            continue
        try:
            lg = sorted(W.game_log(v["id"]), key=lambda g: g["date"])
        except Exception:
            continue
        if lg:
            logs[n] = lg
            byteam[v["team"]].append(n)
    gdate = {g["game_id"]: g["date"][:10] for lg in logs.values() for g in lg}

    dates = {d for d in gdate.values()}
    print(f"fetching ESPN final scores for {len(dates)} game-dates ...")
    margin = _scoreboard(dates)
    print(f"got margins for {len(margin)} team-games\n")

    def played(n, gid):
        return any(g["game_id"] == gid for g in logs.get(n, []))

    def bracketed(n, d):
        ds = [g["date"][:10] for g in logs.get(n, [])]
        return ds and min(ds) < d and max(ds) > d

    spots = []                                       # (margin, over_hit)
    for team, names in byteam.items():
        impact = [n for n in names if pl[n]["min"] >= IMPACT]
        gids = {g["game_id"] for n in names for g in logs[n]}
        for gid in gids:
            d = gdate.get(gid)
            if not d or (d, team) not in margin:
                continue
            outs = [n for n in impact if bracketed(n, d) and not played(n, gid)]
            if not outs:                                 # >=1 impact player out = an injury spot
                continue
            out_logs = [[g for g in logs[o] if g["date"][:10] < d] for o in outs]
            for b in names:
                if b in outs or not played(b, gid):
                    continue
                actual = next(g for g in logs[b] if g["game_id"] == gid)
                if actual["min"] < 12:
                    continue
                bp = [g for g in logs[b] if g["date"][:10] < d]
                if len(bp) < 6:
                    continue
                w = W.wowy_multi(bp, out_logs)
                if w["n_without"] < MIN_NWO or w["n_with"] < 2:
                    continue
                for sk in ("pts", "reb", "ast"):
                    line = round(w["with"][sk]["mean"] * 2) / 2
                    spots.append((margin[(d, team)], 1 if actual[sk] > line else 0))

    print(f"INJURY-OVER spots with a known team margin: {len(spots)}\n")
    buckets = [("blown out (lost by 15+)", lambda m: m <= -15),
               ("lost 5-14", lambda m: -15 < m <= -5),
               ("close (within 4)", lambda m: -5 < m < 5),
               ("won 5-14", lambda m: 5 <= m < 15),
               ("won by 15+", lambda m: m >= 15)]
    print(f"  {'team result':26}{'over hits':>12}{'n':>7}")
    for label, f in buckets:
        rs = [h for m, h in spots if f(m)]
        if rs:
            print(f"  {label:26}{sum(rs)/len(rs)*100:>10.0f}%{len(rs):>7}")
    lost_big = [h for m, h in spots if m <= -10]
    rest = [h for m, h in spots if m > -10]
    print(f"\n  UNDERDOG-BLOWOUT test — team lost by 10+: {sum(lost_big)/len(lost_big)*100:.0f}% over "
          f"(n={len(lost_big)})  vs  everything else {sum(rest)/len(rest)*100:.0f}% (n={len(rest)})")


if __name__ == "__main__":
    main()
