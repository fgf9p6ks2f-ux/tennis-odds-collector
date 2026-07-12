"""Does an injury-driven beneficiary's OVER hit more when the OPPONENT is also missing a key
defender — and does gating on a competitive game rescue the signal the aggregate version buried?

Aggregate opponent-injury was null (weaker-D boost cancelled by the blowout minutes-cap). The sharp
refinement: (1) condition on the opponent missing a POSITIONAL defender (a >=24-mpg starter), and
(2) split by game margin, so the competitive subset isn't contaminated by garbage-time. Leak-free:
for each team-game with >=1 impact player out (our injury-over spot), each beneficiary who played is
bet OVER at their stale with-baseline (WOWY mean with the out-set PLAYING); we then reconstruct which
of the OPPONENT's >=24-mpg rotation players were bracketed-but-absent that game (their missing
defenders) and split the over hit-rate by that + by the beneficiary's team margin.

    python opp_injury_backtest.py
"""
import json
import ssl
import urllib.request
from collections import defaultdict

import wnba_wowy as W

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
IMPACT = 18.0            # our team's out threshold (an injury spot)
DEF_MIN = 24.0          # opponent rotation defender threshold
_PG = {"G": "G", "PG": "G", "SG": "G", "GF": "G", "F": "F", "SF": "F", "PF": "F",
       "FC": "C", "C": "C", "CF": "C"}


def _scoreboard(dates):
    """{(date10, team_abbr): margin} from ESPN finals."""
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
            cs = (e.get("competitions") or [{}])[0].get("competitors", [])
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
    logs, byteam, pos = {}, defaultdict(list), {}
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
            pos[n] = _PG.get(v.get("position", ""), "F")
    gdate = {g["game_id"]: g["date"][:10] for lg in logs.values() for g in lg}
    # who PLAYED each game_id, by team (to find the opponent + its absentees)
    played_by = defaultdict(set)
    for n, lg in logs.items():
        for g in lg:
            played_by[g["game_id"]].add(n)

    print(f"fetching ESPN margins for {len(set(gdate.values()))} dates ...")
    margin = _scoreboard(set(gdate.values()))
    print(f"got {len(margin)} team-game margins\n")

    def played(n, gid):
        return any(g["game_id"] == gid for g in logs.get(n, []))

    def bracketed(n, d):
        ds = [g["date"][:10] for g in logs.get(n, [])]
        return ds and min(ds) < d and max(ds) > d

    spots = []      # (opp_def_out_at_pos, opp_interior_out, opp_perim_out, margin, over_hit)
    for team, names in byteam.items():
        impact = [n for n in names if pl[n]["min"] >= IMPACT]
        gids = {g["game_id"] for n in names for g in logs[n]}
        for gid in gids:
            d = gdate.get(gid)
            if not d:
                continue
            outs = [n for n in impact if bracketed(n, d) and not played(n, gid)]
            if not outs:                                       # our team had an injury spot?
                continue
            out_logs = [[g for g in logs[o] if g["date"][:10] < d] for o in outs]
            # identify the OPPONENT from any teammate's game row for this gid
            opp = None
            for n in names:
                gg = next((g for g in logs[n] if g["game_id"] == gid), None)
                if gg and gg.get("matchup") and gg["matchup"] != team:
                    opp = gg["matchup"]
                    break
            if not opp or opp not in byteam:
                continue
            # OPPONENT's missing >=24-mpg defenders this game, by position group
            opp_out = [n for n in byteam[opp] if pl[n]["min"] >= DEF_MIN
                       and bracketed(n, d) and not played(n, gid)]
            opp_pos = {pos[n] for n in opp_out}
            opp_interior = bool({"C", "F"} & opp_pos)
            opp_perim = "G" in opp_pos
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
                if w["n_without"] < 3 or w["n_with"] < 2:
                    continue
                bpos = pos.get(b, "F")
                at_pos = (bpos in opp_pos)                     # opp missing a defender at BENE's position
                mg = margin.get((d, team))
                for sk in ("pts", "reb", "ast"):
                    line = round(w["with"][sk]["mean"] * 2) / 2
                    hit = 1 if actual[sk] > line else 0
                    # rebounds care about interior; pts/ast about the position matchup
                    rel = opp_interior if sk == "reb" else at_pos
                    spots.append((rel, opp_interior, opp_perim, mg, hit))

    print(f"injury-OVER spots with opponent context: {len(spots)}\n")

    def rate(rows):
        return f"{sum(h for *_, h in rows)/len(rows)*100:.0f}% (n={len(rows)})" if rows else "—"

    yes = [s for s in spots if s[0]]           # opponent missing a relevant defender (pos/interior)
    no = [s for s in spots if not s[0]]
    print("OPPONENT MISSING A RELEVANT DEFENDER (pos-matched; interior for rebounds):")
    print(f"  opp defender OUT : {rate(yes)}")
    print(f"  opp intact       : {rate(no)}")

    print("\nSPREAD-GATED (does removing blowouts rescue it?) — competitive = margin within +/-10:")
    comp = lambda s: s[3] is not None and abs(s[3]) <= 10
    print(f"  opp def OUT · competitive : {rate([s for s in yes if comp(s)])}")
    print(f"  opp def OUT · blowout     : {rate([s for s in yes if s[3] is not None and not comp(s)])}")
    print(f"  opp intact  · competitive : {rate([s for s in no if comp(s)])}")

    print("\nINTERIOR out -> REBOUND overs (boards freed up):")
    reb_int = [s for s in spots if s[1]]       # any interior defender out
    print(f"  (all spots when an opp big is out): {rate(reb_int)}  vs no-big-out {rate([s for s in spots if not s[1]])}")


if __name__ == "__main__":
    main()
