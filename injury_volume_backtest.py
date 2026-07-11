"""The RIGHT test: volume overs driven by a TEAMMATE INJURY, vs real logged FanDuel lines.

The user's edge is causal — a teammate is OUT, their shots redistribute to P, and because the
teammate STAYS out the elevated volume is durable (not a hot-streak that regresses). So this only
looks at spots where, for the game being bet, a KEY teammate was out, and it projects P's points
off P's VOLUME IN THE GAMES WHERE THAT TEAMMATE WAS ALSO OUT (the injury-role analog games) at P's
season efficiency — then grades vs the actual line + box score. Leak-free (only prior games).

    python injury_volume_backtest.py
"""
import json
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

import wnba_regrade as R
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
VOL = json.loads((HERE / "wnba_volume_cache.json").read_text())     # pid -> [games w/ fga/fta/pts/min/date]
DAYS = ("2026-07-07", "2026-07-08", "2026-07-09")
KEY_MIN = 18            # a "key" out teammate plays >=18 min (a real rotation loss)


def games_for(pid):
    return VOL.get(str(pid)) or VOL.get(pid) or []


def main():
    players = W.players()
    id2team = {v["id"]: str(v.get("team", "")) for v in players.values()}
    ids = R._ids()
    # rotation per team + each pid's mean minutes (impact), from the volume cache
    rot, meanmin = defaultdict(set), {}
    for name, v in players.items():
        pid = v["id"]
        g = games_for(pid)
        good = [x for x in g if x["min"] >= 12]
        if len(good) >= 6:
            rot[id2team.get(pid, "")].add(pid)
            meanmin[pid] = st.mean(x["min"] for x in g if x["min"] > 0)
    # team -> date -> set of rotation pids who PLAYED (to derive who was out)
    played = defaultdict(lambda: defaultdict(set))
    for team, pids in rot.items():
        for pid in pids:
            for g in games_for(pid):
                if g["min"] > 0:
                    played[team][g["date"][:10]].add(pid)

    con = sqlite3.connect("fanduel_props.sqlite")
    bets = []
    for date in DAYS:
        line_players = con.execute(
            "SELECT DISTINCT player FROM fd_lines WHERE sport='wnba' AND stat='points' "
            "AND substr(collected_at,1,10)=?", (date,)).fetchall()
        for (player,) in line_players:
            pid = ids.get(player)
            team = id2team.get(pid)
            if not pid or not team or pid not in rot.get(team, ()):
                continue
            games = sorted(games_for(pid), key=lambda g: g["date"])
            gm = next((g for g in games if date <= g["date"][:10] <= _plus(date, 2) and g["min"] > 0), None)
            if not gm:
                continue
            gd = gm["date"][:10]
            # who was OUT for P's team this game = rotation teammates who didn't play (and were
            # recently active, so it's an absence not an off-roster player)
            out_today = [p for p in rot[team] if p != pid and p not in played[team].get(gd, set())
                         and _active(played[team], p, gd)]
            key_out = [p for p in out_today if meanmin.get(p, 0) >= KEY_MIN]
            if not key_out:                                    # NOT an injury spot — skip
                continue
            prior = [g for g in games if g["date"] < gm["date"] and g["min"] > 0]
            if len(prior) < 5:
                continue
            # P's analog games = prior games where a key-out teammate was ALSO out (the injury role)
            analog = [g for g in prior
                      if any(p not in played[team].get(g["date"][:10], set()) for p in key_out)]
            withk = [g for g in prior if g not in analog]
            if len(analog) < 1 or len(withk) < 2:              # need the split to exist
                continue
            ts = sum(g["fga"] + 0.44 * g["fta"] for g in prior)
            pps = sum(g["pts"] for g in prior) / ts if ts else 0
            fga_a = st.median(g["fga"] for g in analog)        # MEDIAN volume — kills outlier games
            fta_a = st.median(g["fta"] for g in analog)
            vol_pts = (fga_a + 0.44 * fta_a) * pps * 0.88      # 0.88 = usage-up-efficiency-down haircut
            # confirm the injury actually lifts P's volume (else it's not their edge)
            fga_with = st.mean(g["fga"] for g in withk)
            if fga_a < fga_with + 1.0:
                continue
            ladder = _ladder(con, player, date)
            picks = [(L, dec) for L, dec in sorted(ladder.items())
                     if vol_pts >= L + 1 and 1.25 <= dec <= 5.0 and L >= 0.4 * vol_pts]
            # keep distinct rungs (>1.5 apart), best price first
            picks.sort(key=lambda x: -x[1])
            kept = []
            for L, dec in picks:
                if not any(abs(k[0] - L) <= 1.5 for k in kept):
                    kept.append((L, dec))
            for L, dec in kept:
                won = gm["pts"] > L
                bets.append({"date": date, "player": player, "line": L, "dec": dec, "vp": vol_pts,
                             "act": gm["pts"], "fga": f"{fga_with:.1f}->{fga_a:.1f}",
                             "nanalog": len(analog), "kout": len(key_out),
                             "won": won, "u": (dec - 1) if won else -1.0})
    con.close()

    if not bets:
        print("no injury-driven volume overs in the logged-line window")
        return
    print(f"\nINJURY-DRIVEN VOLUME POINTS OVERS vs REAL FanDuel lines — {DAYS[0]}..{DAYS[-1]}\n")
    print(f"  {'date':11}{'player':19}{'bet':>8}{'odds':>7}{'proj':>6}{'act':>5}"
          f"{'FGA w/wo':>11}{'#an':>4}  W/L")
    w = 0
    for b in sorted(bets, key=lambda b: (b["date"], b["player"])):
        am = f'+{round((b["dec"]-1)*100)}' if b["dec"] >= 2 else f'-{round(100/(b["dec"]-1))}'
        w += b["won"]
        print(f'  {b["date"]:11}{b["player"][:18]:19}{"o"+format(b["line"],"g"):>8}{am:>7}'
              f'{b["vp"]:>6.1f}{b["act"]:>5.0f}{b["fga"]:>11}{b["nanalog"]:>4}  {"WIN " if b["won"] else "loss"}')
    u = sum(b["u"] for b in bets)
    print(f'\n  record {w}-{len(bets)-w} ({100*w/len(bets):.0f}%)  ·  {u:+.2f}u  ·  '
          f'ROI {100*u/len(bets):+.1f}%  (n={len(bets)})')


def _active(pl_by_date, pid, date, win=24):
    import datetime as dt
    lo = (dt.date.fromisoformat(date) - dt.timedelta(days=win)).isoformat()
    return any(lo <= d < date and pid in s for d, s in pl_by_date.items())


def _ladder(con, player, date):
    rows = con.execute("SELECT line, side, odds, collected_at FROM fd_lines WHERE sport='wnba' "
                       "AND stat='points' AND player=? AND substr(collected_at,1,10)=?",
                       (player, date)).fetchall()
    best = {}
    for line, side, odds, ca in rows:
        if side == "over" and line is not None:
            k = round(float(line), 1)
            if k not in best or ca > best[k][1]:
                best[k] = (float(odds), ca)
    return {k: v[0] for k, v in best.items()}


def _plus(date, days):
    import datetime as dt
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


if __name__ == "__main__":
    main()
