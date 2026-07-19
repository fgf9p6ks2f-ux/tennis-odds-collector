"""Opposing-bigs REB shadow logger — collect-only instrument, never bets, never pings.

Verdict trail (2026-07-19 deep validation): the user's center-out → opposing-bigs thesis
survives ONLY as bigs-REBOUNDS (points failed the guards placebo), and even that is
UNSTABLE across six seasons (per-season lift vs control: +5.9/+1.5/+2.4/-0.8/-5.5/+4.3,
median delta 0.0). Real FD lines since 7/7: center-out 12-8 (60%) +1.98u vs control
25-22 (53%) — +7pts but n=20. Not shippable; not dead. This logger records every
center-out opposing-big REB main line daily so the live sample reaches a real verdict
(~60-80 spots). Reviewed at the checkpoint alongside the capped-legs csv.

Appends to wnba_oppbig_shadow.csv: date, out_center, team, opp_big, line, over_odds.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

import wnba_tonight as T
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
OUT = HERE / "wnba_oppbig_shadow.csv"


def main():
    pl = W.players()
    inj = T.injuries()
    matchups = T.tonight_matchups()
    playing = set(matchups)
    today = dt.datetime.now(T.ET).date().isoformat()
    try:
        pos = {int(k): v for k, v in json.loads((HERE / "wnba_positions.json").read_text()).items()}
    except (OSError, ValueError):
        pos = {}
    seen = set()
    if OUT.exists():
        with OUT.open() as f:
            seen = {(r[0], r[3]) for r in csv.reader(f) if r}
    n = 0
    for name, status in inj.items():
        p = pl.get(name)
        if (status not in ("Out", "Doubtful") or not p or p["team"] not in playing
                or p["min"] < 25 or "C" not in pos.get(p.get("id"), "")):
            continue
        opp = matchups.get(p["team"])
        if not opp:
            continue
        for nm, v in pl.items():
            if v["team"] != opp or v["gp"] < 5 or v["reb"] < 5:
                continue
            if (today, nm) in seen:
                continue
            pn = T.posted_props(nm)
            reb = (pn or {}).get("rebounds") or {}
            main = min(reb.items(), key=lambda kv: abs((kv[1][0] or 9) - 1.90), default=None)
            if not main or not main[1][0]:
                continue
            with OUT.open("a", newline="") as f:
                csv.writer(f).writerow([today, name, p["team"], nm, main[0], main[1][0]])
            seen.add((today, nm))
            n += 1
    if n:
        print(f"oppbig shadow: logged {n} spot(s)")


if __name__ == "__main__":
    main()
