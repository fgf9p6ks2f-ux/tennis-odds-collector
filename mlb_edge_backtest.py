"""Approximate EDGE backtest for the MLB lineup-slot play — does it beat a stale line?

We have no historical prop lines, so we PROXY the book: it sets the total-bases line from
the hitter's SEASON-BLEND rate (across all slots — the 'stale anchor' we believe books
lean on), priced with a realistic ~5% hold. Then we bet the OVER only when a hitter who
NORMALLY bats lower (usual slot >=4) batted TOP-3 that game — the 'moved up because a
regular sat' scenario. Grade vs actual TB. If the lineup effect isn't priced in, +EV.

Efficient + honest: iterate each season's games ONCE (one boxscore per game yields every
batter's slot + TB), not per-hitter. The anchor is the hitter's full-season rate = exactly
what a book prices off, so betting their elevated games against it is the real test.

CAVEATS: proxy line (not the true posted line/odds); assumes the book doesn't adjust for
slot (if it partly does, the real edge is smaller); TB is high-variance. Directional.

    python mlb_edge_backtest.py --seasons 2025
"""
from __future__ import annotations

import argparse
import time

import requests

API = "https://statsapi.mlb.com/api/v1"
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"}
LINES = [0.5, 1.5, 2.5, 3.5]
HOLD = 0.05                         # ~two-way vig baked into the offered over price
MIN_GAMES = 40                     # season sample to trust a hitter's anchor
MIN_TOP3 = 8                       # need a real set of "moved up" games to bet


def _get(url):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=H, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.0 * (attempt + 1))
    return None


def game_pks(season):
    j = _get(f"{API}/schedule?sportId=1&season={season}&gameType=R") or {}
    pks = []
    for d in j.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("codedGameState") == "F":     # final only
                pks.append(g["gamePk"])
    return pks


def _tb(b):
    if b.get("totalBases") is not None:
        return int(b["totalBases"])
    h, d, t, hr = (int(b.get(k, 0) or 0) for k in ("hits", "doubles", "triples", "homeRuns"))
    return (h - d - t - hr) + 2 * d + 3 * t + 4 * hr           # singles + 2b*2 + 3b*3 + hr*4


def collect(season, progress_every=250):
    """{pid: [(slot, tb), ...]} for every batter over the season."""
    pks = game_pks(season)
    print(f"  {season}: {len(pks)} final games")
    by_player = {}
    for i, pk in enumerate(pks):
        bs = _get(f"{API}/game/{pk}/boxscore")
        if not bs:
            continue
        for side in ("home", "away"):
            for key, pl in bs.get("teams", {}).get(side, {}).get("players", {}).items():
                order = pl.get("battingOrder")
                bat = pl.get("stats", {}).get("batting", {})
                if not order or not bat or bat.get("plateAppearances") in (None, 0):
                    continue
                slot = int(order) // 100
                if not 1 <= slot <= 9:
                    continue
                by_player.setdefault(pl["person"]["id"], []).append((slot, _tb(bat)))
        if (i + 1) % progress_every == 0:
            print(f"    ...{i+1}/{len(pks)} games")
        time.sleep(0.03)
    return by_player


def backtest(seasons):
    bets = []                       # (win, offered_dec)
    raw = []                        # (top3_rate, season_rate) for the pure signal
    for season in seasons:
        for pid, games in collect(season).items():
            if len(games) < MIN_GAMES:
                continue
            slots = [s for s, _ in games]
            usual = max(set(slots), key=slots.count)
            if usual <= 3:
                continue            # normally bats top -> book already prices it, no edge
            top3 = [tb for s, tb in games if s <= 3]
            if len(top3) < MIN_TOP3:
                continue
            all_tb = [tb for _, tb in games]
            # the line the book would center on the hitter's SEASON blend
            L = min(LINES, key=lambda x: abs(sum(v > x for v in all_tb) / len(all_tb) - 0.5))
            season_over = sum(v > L for v in all_tb) / len(all_tb)
            if not 0.15 < season_over < 0.85:
                continue
            offered_dec = 1.0 / (season_over + HOLD / 2)          # vig -> worse payout
            top3_over = sum(v > L for v in top3) / len(top3)
            raw.append((top3_over, season_over))
            for tb in top3:                                       # bet the over in each
                bets.append((tb > L, offered_dec))
    return bets, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2025")
    args = ap.parse_args()
    seasons = [int(s) for s in args.seasons.split(",")]
    print(f"MLB lineup-slot EDGE backtest (season-anchored proxy line) {seasons}\n")
    bets, raw = backtest(seasons)
    if not bets:
        print("no qualifying bets")
        return
    n = len(bets)
    wins = sum(1 for w, _ in bets if w)
    units = sum((d - 1) if w else -1 for w, d in bets)
    tr = sum(r[0] for r in raw) / len(raw)
    sr = sum(r[1] for r in raw) / len(raw)
    print(f"\n=== RESULT ===")
    print(f"{n} over-bets on 'moved-up' hitters (usual slot >=4, batted top-3)")
    print(f"record {wins}-{n-wins} · win {wins/n*100:.1f}% · {units:+.1f}u (${units*100:+.0f}) "
          f"· ROI {units/n*100:+.1f}%")
    print(f"raw signal: top-3 over-rate {tr*100:.1f}% vs season-anchor {sr*100:.1f}% "
          f"(+{(tr-sr)*100:.1f} pts) over {len(raw)} hitter-seasons")
    print(f"  ROI is AFTER a {HOLD*100:.0f}% vig haircut. Proxy line, not real odds — directional.")


if __name__ == "__main__":
    main()
