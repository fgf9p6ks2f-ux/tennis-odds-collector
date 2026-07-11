"""VOLUME-BASED POINTS projection — the user's real laddering edge.

Thesis: shooting % is variance, VOLUME (FGA + FTA) is sticky (role/minutes-driven). So when an
injury hands a player a bigger role, project their points off the elevated VOLUME at their normal
efficiency — NOT off recent points (which carry single-game shooting noise the book fades). A
player who jumps to 12 FGA / 4 FTA has a ~10-14 pt floor even on a 3-for-12 night. When the book
anchors the line near the season average and the volume confirms the role, the over — and the
LADDER up the alt lines — is live.

This backtests, leak-free:
  1. does a VOLUME projection predict points better than a recent-POINTS projection (esp. in
     volume-jump spots)?
  2. against a STALE line (book anchored to season avg), do volume-confirmed overs win — and how
     far up the LADDER (+3 / +6) do they stay profitable?

    python volume_points_backtest.py [test_days]
"""
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import requests

import wnba_backtest_layers as B

HERE = Path(__file__).resolve().parent
BOX = HERE / "wnba_box_cache.json"
VOL = HERE / "wnba_volume_cache.json"
UA = {"User-Agent": "Mozilla/5.0"}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _ma(s):
    try:
        a, b = str(s).split("-")
        return _num(a), _num(b)
    except ValueError:
        return 0.0, 0.0


def gamelog(pid, cache):
    if pid in cache:
        return cache[pid]
    try:
        d = requests.get(f"https://site.api.espn.com/apis/common/v3/sports/basketball/wnba/"
                         f"athletes/{pid}/gamelog", headers=UA, timeout=30).json()
    except Exception:
        cache[pid] = []
        return []
    ev = d.get("events", {})
    rows = []
    for stype in d.get("seasonTypes", []):
        for cat in stype.get("categories", []):
            for e in cat.get("events", []):
                gid, s = e.get("eventId"), e.get("stats", [])
                if gid not in ev or len(s) < 14:
                    continue
                fgm, fga = _ma(s[7])
                tpm, tpa = _ma(s[9])
                ftm, fta = _ma(s[11])
                rows.append({"date": ev[gid].get("gameDate", "")[:10], "min": _num(s[0]),
                             "pts": _num(s[1]), "fga": fga, "fgm": fgm, "tpa": tpa, "tpm": tpm,
                             "fta": fta, "ftm": ftm})
    rows = [r for r in rows if r["min"] > 0]
    rows.sort(key=lambda r: r["date"])
    cache[pid] = rows
    return rows


def rate_proj(prior, key, proj_min, n=5):
    """recent per-minute rate x projected minutes (volume/points projected to the elevated role)."""
    last = prior[-n:]
    r = st.mean(g[key] / max(g["min"], 1) for g in last)
    return r * proj_min


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    box = json.loads(BOX.read_text()) if BOX.exists() else {}
    cache = json.loads(VOL.read_text()) if VOL.exists() else {}
    # rotation pids = appeared >=6 games at >=12 min in the box cache
    appear = defaultdict(int)
    for rows in box.values():
        for pid, team, opp, d in rows:
            if d.get("min", 0) >= 12:
                appear[pid] += 1
    pids = [p for p, c in appear.items() if c >= 6]
    n0 = len(cache)
    for pid in pids:
        gamelog(pid, cache)
    if len(cache) != n0:
        VOL.write_text(json.dumps(cache))

    all_dates = sorted({r["date"] for rows in cache.values() for r in rows})
    if not all_dates:
        print("no volume data")
        return
    cutoff = all_dates[-min(days, len(all_dates))]

    mae = {"raw": [], "vol": []}
    jmae = {"raw": [], "vol": []}                      # volume-jump subset
    # over hit for each projector on a STALE line (season-anchored), all + jump subset
    hit = {m: {"n": 0, "w": 0} for m in ("raw", "vol")}
    jhit = {m: {"n": 0, "w": 0} for m in ("raw", "vol")}     # vs fully-stale line
    jhit_book = {m: {"n": 0, "w": 0} for m in ("raw", "vol")}   # vs realistically-adjusted line
    ladder = {0: [0, 0], 3: [0, 0], 6: [0, 0]}         # vol JUMP overs vs BOOK line: [wins,n] at +k
    njump = 0
    for pid, games in cache.items():
        for i, g in enumerate(games):
            if g["date"] < cutoff or i < 6:
                continue
            prior = games[:i]
            base = prior[:-3] if len(prior) >= 6 else prior      # PRE-SURGE baseline (what the
            base_fga = st.mean(x["fga"] for x in base)           # book anchors the line to)
            base_pts = st.mean(x["pts"] for x in base)
            base_min = st.mean(x["min"] for x in base)
            season_fga = st.mean(x["fga"] for x in prior)
            season_pts = st.mean(x["pts"] for x in prior)
            fga_sum = sum(x["fga"] for x in prior)
            tpa_sum = sum(x["tpa"] for x in prior)
            fgm_sum = sum(x["fgm"] for x in prior)
            tpm_sum = sum(x["tpm"] for x in prior)
            fta_sum = sum(x["fta"] for x in prior)
            ftm_sum = sum(x["ftm"] for x in prior)
            # LIVE-computable efficiency: points per true-shot (game log has ATTEMPTS, not makes)
            pps_ts = sum(x["pts"] for x in prior) / max(sum(x["fga"] + 0.44 * x["fta"] for x in prior), 1)
            proj_min = st.mean(x["min"] for x in prior[-5:])
            fga_p = rate_proj(prior, "fga", proj_min)
            fta_p = rate_proj(prior, "fta", proj_min)
            vol_pts = (fga_p + 0.44 * fta_p) * pps_ts
            raw_pts = rate_proj(prior, "pts", proj_min)
            actual = g["pts"]
            recent_fga = st.mean(x["fga"] for x in prior[-3:])
            recent_min = st.mean(x["min"] for x in prior[-3:])
            # the user's spot: a GENUINE role elevation — minutes AND shot volume both jumped vs the
            # pre-surge baseline, off a player who wasn't already a big-minutes starter.
            jump = (recent_fga >= 1.4 * max(base_fga, 0.1) and recent_min >= base_min + 5
                    and base_min < 26)
            mae["raw"].append(abs(raw_pts - actual))
            mae["vol"].append(abs(vol_pts - actual))
            if jump:
                njump += 1
                jmae["raw"].append(abs(raw_pts - actual))
                jmae["vol"].append(abs(vol_pts - actual))
            recent_pts = st.mean(x["pts"] for x in prior[-3:])
            line = math.floor(base_pts) + 0.5                    # STALE: fully anchored to baseline
            # REALISTIC book line: baseline bumped ~35% toward the surge (user's 5ppg -> 7.5 line)
            line_book = math.floor(base_pts + 0.35 * (recent_pts - base_pts)) + 0.5
            for m, pr in (("raw", raw_pts), ("vol", vol_pts)):
                if pr >= line + 1:                               # projector flags an over
                    hit[m]["n"] += 1
                    hit[m]["w"] += 1 if actual > line else 0
                    if jump:
                        jhit[m]["n"] += 1
                        jhit[m]["w"] += 1 if actual > line else 0
                if jump and pr >= line_book + 1:                 # flags over the REALISTIC line
                    jhit_book[m]["n"] += 1
                    jhit_book[m]["w"] += 1 if actual > line_book else 0
            if jump and vol_pts >= line_book + 1:                # the user's spot, realistic line
                for k in ladder:
                    ladder[k][1] += 1
                    ladder[k][0] += 1 if actual > line_book + k else 0

    print(f"\nVOLUME-POINTS BACKTEST — last {days} days, {len(mae['raw'])} points spots "
          f"({njump} volume-jump)\n")
    print(f"  {'MAE':14}{'recent-pts':>12}{'volume':>10}")
    print(f"  {'all spots':14}{st.mean(mae['raw']):>12.2f}{st.mean(mae['vol']):>10.2f}")
    if jmae["raw"]:
        print(f"  {'volume-jump':14}{st.mean(jmae['raw']):>12.2f}{st.mean(jmae['vol']):>10.2f}")
    def be(p):      # fair American odds for hit-prob p (favorite if p>.5)
        if not p or p >= 1:
            return "-"
        return f"-{round(100*p/(1-p))}" if p > 0.5 else f"+{round(100*(1-p)/p)}"

    print(f"\n  volume-jump OVER hit rate (breakeven ~52.4% at -110):")
    for label, dd in (("vs fully-stale line", jhit), ("vs realistic +35% line", jhit_book)):
        for m in ("raw", "vol"):
            o = dd[m]
            r = 100 * o["w"] / o["n"] if o["n"] else float("nan")
            print(f"    {label:24} {m:4}: {r:5.1f}%  fair {be(o['w']/o['n']) if o['n'] else '-':>6}  (n{o['n']})")
    print(f"\n  LADDER — vol-flagged jump overs, vs the REALISTIC line (needs the book's alt line to")
    print(f"  pay BETTER than 'fair' to profit — your 'books are lazy on alt lines' claim):")
    print(f"    {'rung':10}{'hit%':>8}{'fair odds':>12}")
    for k in sorted(ladder):
        w, n = ladder[k]
        if n:
            p = w / n
            print(f"    line+{k:<5}{100*p:>7.1f}%{be(p):>12}  (n{n})")


if __name__ == "__main__":
    main()
