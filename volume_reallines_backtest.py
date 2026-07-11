"""Backtest the VOLUME points-over model against the ACTUAL logged FanDuel lines + odds (fd_lines,
7/7-7/9 finished games). Leak-free: project each spot off games strictly before it. Replicates the
LIVE flagging (volume_points + the prop_edges volume branch) exactly, then grades vs the real box
score at the real price. Small sample (the feed is only days old) — shown bet-by-bet, honestly.

    python volume_reallines_backtest.py
"""
import json
import math
import sqlite3
import statistics as st
from pathlib import Path

import wnba_regrade as R
import wnba_tonight as T

HERE = Path(__file__).resolve().parent
VOL = json.loads((HERE / "wnba_volume_cache.json").read_text())     # pid -> [games] (has date/pts/fga/fta/min)
SHRINK_K = 6
DAYS = ("2026-07-07", "2026-07-08", "2026-07-09")


def real_ladder(con, player, date):
    """{line: over_dec} from the LATEST collection that day for this player's points."""
    rows = con.execute("SELECT line, side, odds, collected_at FROM fd_lines WHERE sport='wnba' "
                       "AND stat='points' AND player=? AND substr(collected_at,1,10)=?",
                       (player, date)).fetchall()
    best = {}
    for line, side, odds, ca in rows:
        if side != "over" or line is None:
            continue
        k = round(float(line), 1)
        if k not in best or ca > best[k][1]:
            best[k] = (float(odds), ca)
    return {k: v[0] for k, v in best.items()}


def games_for(pid):
    return VOL.get(str(pid)) or VOL.get(pid) or []


def _robust_vp(prior, proj_min):
    """ROBUST volume projection: MEDIAN per-min shot rate (kills outlier games like a 53-pt night),
    shrunk 25% toward season points (regression), confirmed only on a real FGA *and* minutes jump."""
    g = sorted(prior, key=lambda x: x["date"])
    ts = sum(x["fga"] + 0.44 * x["fta"] for x in g)
    if ts <= 0:
        return None
    pps = sum(x["pts"] for x in g) / ts
    rec = g[-4:]
    fga_r = st.median(x["fga"] / max(x["min"], 1) for x in rec)
    fta_r = st.median(x["fta"] / max(x["min"], 1) for x in rec)
    vol = (fga_r + 0.44 * fta_r) * proj_min * pps
    season = st.mean(x["pts"] for x in g)
    vol = 0.75 * vol + 0.25 * season                       # regress toward the season number
    base, r3 = g[:-3], g[-3:]
    bf, rf = st.mean(x["fga"] for x in base), st.mean(x["fga"] for x in r3)
    bm, rm = st.mean(x["min"] for x in base), st.mean(x["min"] for x in r3)
    return {"vol_pts": vol, "confirmed": rf >= 1.35 * bf and rm >= bm + 4,
            "sigma": max(st.pstdev([x["pts"] for x in g[-8:]]), 4.0),
            "base_fga": round(bf, 1), "recent_fga": round(rf, 1)}


def main():
    ids = R._ids()
    con = sqlite3.connect("fanduel_props.sqlite")
    bets = []
    for date in DAYS:
        players = [r[0] for r in con.execute(
            "SELECT DISTINCT player FROM fd_lines WHERE sport='wnba' AND stat='points' "
            "AND substr(collected_at,1,10)=?", (date,))]
        for player in players:
            pid = ids.get(player)
            games = games_for(pid)
            if not games:
                continue
            # the game this line was for = earliest game on/after the line date (within 2 days)
            gm = next((g for g in sorted(games, key=lambda g: g["date"])
                       if date <= g["date"][:10] <= _plus(date, 2)), None)
            if not gm or gm["min"] <= 0:
                continue
            prior = [g for g in games if g["date"] < gm["date"] and g["min"] > 0]
            if len(prior) < 6:
                continue
            proj_min = st.mean(g["min"] for g in prior[-5:])
            vp = _robust_vp(prior, proj_min)
            if not vp or not vp["confirmed"]:
                continue
            elev = [g for g in prior if g["min"] >= max(proj_min - 4, 22)]
            n = max(len(elev), 4)
            ladder = real_ladder(con, player, date)
            picks = []
            for line, dec in sorted(ladder.items()):
                if vp["vol_pts"] < line or not (1.25 <= dec <= 5.0):
                    continue
                if line < 0.4 * vp["vol_pts"]:                       # deep-favorite junk
                    continue
                hit = T._norm_sf((line - vp["vol_pts"]) / vp["sigma"])
                if hit >= 0.92 and dec >= 2.0:
                    continue
                p_adj = (hit * n + (1 / dec) * SHRINK_K) / (n + SHRINK_K)
                ev = p_adj * dec - 1
                if ev >= T.VOL_EV_MIN:
                    picks.append((line, dec, hit, ev))
            # collapse adjacent rungs (within 1.5) to best-EV, like prop_edges
            picks.sort(key=lambda x: -x[3])
            kept = []
            for p in picks:
                if not any(abs(k[0] - p[0]) <= 1.5 for k in kept):
                    kept.append(p)
            for line, dec, hit, ev in kept:
                won = gm["pts"] > line
                bets.append({"date": date, "player": player, "line": line, "dec": dec,
                             "vp": vp["vol_pts"], "actual": gm["pts"], "fga_j": f'{vp["base_fga"]}->{vp["recent_fga"]}',
                             "won": won, "u": (dec - 1) if won else -1.0})
    con.close()

    if not bets:
        print("no volume-confirmed points overs in the logged-line window")
        return
    print(f"\nVOLUME POINTS OVERS vs REAL FanDuel lines — {DAYS[0]}..{DAYS[-1]}\n")
    print(f"  {'date':11}{'player':20}{'bet':>9}{'odds':>7}{'proj':>6}{'act':>5}{'FGA':>10}  W/L")
    w = 0
    for b in sorted(bets, key=lambda b: (b["date"], b["player"])):
        am = f'+{round((b["dec"]-1)*100)}' if b["dec"] >= 2 else f'-{round(100/(b["dec"]-1))}'
        w += b["won"]
        print(f'  {b["date"]:11}{b["player"][:19]:20}{"o"+format(b["line"],"g"):>9}{am:>7}'
              f'{b["vp"]:>6.1f}{b["actual"]:>5.0f}{b["fga_j"]:>10}  {"WIN " if b["won"] else "loss"}')
    u = sum(b["u"] for b in bets)
    print(f'\n  record {w}-{len(bets)-w} ({100*w/len(bets):.0f}%)  ·  {u:+.2f}u flat  ·  '
          f'ROI {100*u/len(bets):+.1f}%  (n={len(bets)}, TINY — directional only)')


def _plus(date, days):
    import datetime as dt
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


if __name__ == "__main__":
    main()
