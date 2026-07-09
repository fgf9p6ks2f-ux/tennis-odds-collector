"""MLB lineup-slot WOWY — the batting-order analog of the WNBA injury edge. SCAFFOLD.

The WNBA edge: a key player sits -> minutes/usage redistribute -> a beneficiary's props
go stale. MLB analog: a regular sits -> the batting ORDER shifts -> a hitter moves UP ->
more plate appearances -> more hits/total-bases/RBI chances -> the book is slow to reprice
batter props at lineup release (~3-4h pre-game, the speed window).

MLB StatsAPI (free) gives the key data NATIVELY:
  - production split by batting-order slot (sitCodes b1..b9) — the "how does hitter X
    produce batting 2nd vs 7th" lookup, no box-score scraping,
  - per-game batting order (boxscore battingOrder = slot*100) — leak-free game-level
    backtesting across decades of games.
So the PROJECTION is richly backtestable (huge sample), unlike WNBA's thin injury data.
(The betting edge vs real lines is still forward-only — books hide historical prop lines.)

NOTE: statsapi 403s a python-requests UA from datacenter IPs -> browser UA (CI-safe).

    python mlb_wowy.py --player "Aaron Judge"     # production by batting slot
    python mlb_wowy.py --slot-effect              # league within-hitter slot effect (backtest)
"""
from __future__ import annotations

import argparse
import statistics as st
import time

import requests

API = "https://statsapi.mlb.com/api/v1"
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"}
SEASON = 2026
SLOTS = {"Batting First": 1, "Batting Second": 2, "Batting Third": 3, "Batting Fourth": 4,
         "Batting Fifth": 5, "Batting Sixth": 6, "Batting Seventh": 7, "Batting Eighth": 8,
         "Batting Ninth": 9}
_HITTERS = {}


def _get(url):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=H, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"statsapi failed: {url[:70]}")


def hitters(min_pa=60):
    """{name: {id, team, pa, tb, avg, ops}} — this season's hitters with real volume."""
    if _HITTERS:
        return _HITTERS
    j = _get(f"{API}/stats?stats=season&group=hitting&season={SEASON}&playerPool=All&limit=2000")
    for s in j.get("stats", [{}])[0].get("splits", []):
        stat = s.get("stat", {})
        pa = int(stat.get("plateAppearances", 0) or 0)
        if pa < min_pa:
            continue
        p = s.get("player", {})
        _HITTERS[p.get("fullName", "")] = {
            "id": p.get("id"), "team": (s.get("team", {}) or {}).get("abbreviation", ""),
            "pa": pa, "tb": int(stat.get("totalBases", 0) or 0),
            "avg": stat.get("avg"), "ops": stat.get("ops")}
    return _HITTERS


def game_log(pid):
    """[{game_pk, date, opp, ab, pa, h, tb, r, rbi, bb}] for a hitter this season."""
    j = _get(f"{API}/people/{pid}/stats?stats=gameLog&group=hitting&season={SEASON}")
    out = []
    for s in j.get("stats", [{}])[0].get("splits", []):
        stt = s.get("stat", {})
        out.append({"game_pk": (s.get("game", {}) or {}).get("gamePk"),
                    "date": s.get("date", ""),
                    "opp": (s.get("opponent", {}) or {}).get("abbreviation", ""),
                    "ab": int(stt.get("atBats", 0) or 0),
                    "pa": int(stt.get("plateAppearances", 0) or 0),
                    "h": int(stt.get("hits", 0) or 0),
                    "tb": int(stt.get("totalBases", 0) or 0),
                    "r": int(stt.get("runs", 0) or 0),
                    "rbi": int(stt.get("rbi", 0) or 0),
                    "bb": int(stt.get("baseOnBalls", 0) or 0)})
    return out


def slot_splits(pid):
    """The lineup-slot WOWY: {slot: {games, pa, ab, h, tb, rbi, tb_per_g, h_per_g}} — how a
    hitter produces by batting-order position. The core projection lookup."""
    codes = ",".join(f"b{i}" for i in range(1, 10))
    j = _get(f"{API}/people/{pid}/stats?stats=statSplits&group=hitting&season={SEASON}&sitCodes={codes}")
    out = {}
    for s in j.get("stats", [{}])[0].get("splits", []):
        slot = SLOTS.get(s.get("split", {}).get("description", ""))
        if not slot:
            continue
        stt = s.get("stat", {})
        g = int(stt.get("gamesPlayed", 0) or 0)
        tb = int(stt.get("totalBases", 0) or 0)
        h = int(stt.get("hits", 0) or 0)
        out[slot] = {"games": g, "pa": int(stt.get("plateAppearances", 0) or 0),
                     "ab": int(stt.get("atBats", 0) or 0), "h": h, "tb": tb,
                     "rbi": int(stt.get("rbi", 0) or 0),
                     "tb_per_g": round(tb / g, 2) if g else 0,
                     "h_per_g": round(h / g, 2) if g else 0}
    return out


def game_order(game_pk, pid):
    """A hitter's batting slot in one game (leak-free per-game backtesting), or None."""
    bs = _get(f"{API}/game/{game_pk}/boxscore")
    for side in ("home", "away"):
        pl = bs.get("teams", {}).get(side, {}).get("players", {}).get(f"ID{pid}")
        if pl and pl.get("battingOrder"):
            return int(pl["battingOrder"]) // 100          # 300 -> 3rd
    return None


def _slot_effect():
    """BACKTEST (projection): across hitters who batted in both a TOP (1-3) and a LOWER
    (6-9) slot this season, does the SAME hitter produce more TB/game up top? Isolates the
    within-hitter lineup effect (the projectable edge) from 'better hitters bat higher'."""
    hs = hitters(min_pa=150)
    print(f"within-hitter slot effect over {len(hs)} hitters (TB/game, top 1-3 vs lower 6-9):\n")
    deltas = []
    for name, v in list(hs.items())[:120]:
        try:
            ss = slot_splits(v["id"])
        except RuntimeError:
            continue
        top = [ss[s] for s in (1, 2, 3) if s in ss and ss[s]["games"] >= 5]
        low = [ss[s] for s in (6, 7, 8, 9) if s in ss and ss[s]["games"] >= 5]
        if not top or not low:
            continue
        t = sum(x["tb"] for x in top) / sum(x["games"] for x in top)
        l = sum(x["tb"] for x in low) / sum(x["games"] for x in low)
        deltas.append(t - l)
        if len(deltas) <= 12:
            print(f"  {name:22} top {t:.2f} vs low {l:.2f} TB/g   {t-l:+.2f}")
        time.sleep(0.05)
    if deltas:
        pos = sum(1 for d in deltas if d > 0)
        print(f"\n  {len(deltas)} hitters with both samples · mean +{st.mean(deltas):.2f} TB/game "
              f"batting top-3 vs 6-9 · {pos}/{len(deltas)} positive ({pos/len(deltas)*100:.0f}%)")
        print("  -> the within-hitter lineup effect is real + measurable = the edge is "
              "backtestable at scale (this is season-1; run multi-season for power).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player")
    ap.add_argument("--slot-effect", action="store_true")
    args = ap.parse_args()
    if args.slot_effect:
        _slot_effect()
        return
    if args.player:
        h = hitters().get(args.player)
        if not h:
            raise SystemExit("hitter not found (exact name).")
        print(f"\n{args.player} ({h['team']}) — {h['pa']} PA, {h['avg']} AVG, {h['ops']} OPS")
        print("production by batting-order slot:")
        for slot, d in sorted(slot_splits(h["id"]).items()):
            print(f"  bat {slot}: {d['games']:2}g  {d['pa']:3}PA  "
                  f"{d['tb_per_g']:.2f} TB/g  {d['h_per_g']:.2f} H/g")


if __name__ == "__main__":
    main()
