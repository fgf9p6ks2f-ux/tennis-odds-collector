#!/usr/bin/env python3
"""FREE-TIER go/no-go check for The Odds API before buying the historical archive. Hits ONLY the live
endpoints (all the free tier allows) to confirm, for MLB and WNBA, that the exact market keys the two
backtest harnesses need are posted RIGHT NOW with FanDuel + DraftKings. Deliberately economical: one
event per sport, so it burns ~50-90 of the 500 free monthly credits.

Key: reads THE_ODDS_API_KEY from env, else from a gitignored `.odds_key` file next to this script.
"""
import os
import sys
from pathlib import Path

import requests   # bundles certs (certifi) — stock-Python urllib fails cert verify on this Mac

API = "https://api.the-odds-api.com/v4"
HERE = Path(__file__).resolve().parent

MLB_MARKETS = ["pitcher_outs"]
WNBA_MARKETS = ["player_points", "player_rebounds", "player_assists",
                "player_points_rebounds_assists", "player_points_rebounds",
                "player_points_assists", "player_rebounds_assists"]
WANT_BOOKS = {"fanduel", "draftkings"}


def _key():
    k = os.environ.get("THE_ODDS_API_KEY")
    if not k:
        f = HERE / ".odds_key"
        if f.exists():
            k = f.read_text().strip()
    if not k:
        sys.exit("no key: `export THE_ODDS_API_KEY=...` or put it in ./.odds_key (gitignored).")
    return k


def _get(path, **params):
    params["apiKey"] = _key()
    try:
        r = requests.get(f"{API}{path}", params=params,
                         headers={"User-Agent": "odds-verify"}, timeout=30)
    except Exception as e:
        return None, None, str(e)
    rem = r.headers.get("x-requests-remaining")
    if r.status_code != 200:
        return None, rem, f"{r.status_code} {r.text[:200]}"
    return r.json(), rem, None


def check(sport, markets, label, probe=3):
    """Probe the SOONEST few games (that's where props are live — books post a few hours before tip)
    and AGGREGATE which books/markets appear across them, so one early game missing FanDuel doesn't
    read as a coverage gap."""
    print(f"\n══ {label} ({sport}) ══")
    events, rem, err = _get(f"/sports/{sport}/events")
    if err:
        print(f"  ✖ events call failed: {err}")
        return False
    events = sorted(events or [], key=lambda e: e.get("commence_time") or "")
    print(f"  events posted: {len(events)}   [free credits remaining: {rem}]")
    if not events:
        print("  (no games on the board — retry when a slate is up)")
        return None
    soon = [e.get("commence_time", "")[:16] for e in events[:4]]
    print(f"  soonest games: {soon}")
    books_all, markets_all, samples = set(), set(), []
    probed = 0
    for ev in events[:probe]:
        data, rem, err = _get(f"/sports/{sport}/events/{ev['id']}/odds", regions="us",
                              markets=",".join(markets), oddsFormat="decimal",
                              bookmakers="fanduel,draftkings")
        if err:
            continue
        probed += 1
        got = False
        for bk in (data or {}).get("bookmakers") or []:
            if bk.get("key") not in WANT_BOOKS:
                continue
            for mk in bk.get("markets") or []:
                books_all.add(bk["key"])
                markets_all.add(mk.get("key"))
                got = True
                for o in (mk.get("outcomes") or [])[:1]:
                    if len(samples) < 8:
                        samples.append(f"{bk['key']:10s} {mk['key']:32s} "
                                       f"{o.get('description') or o.get('name')} "
                                       f"{o.get('name')} {o.get('point')} @ {o.get('price')}")
        if got:
            print(f"  probed {ev.get('away_team')} @ {ev.get('home_team')} "
                  f"({ev.get('commence_time','')[:16]}) → props present [credits {rem}]")
    print(f"  ─ aggregated over {probed} probed game(s) ─")
    print(f"  books seen  : {sorted(books_all) or '— none —'}   (want {sorted(WANT_BOOKS)})")
    print(f"  markets seen: {sorted(markets_all) or '— none —'}")
    if samples:
        print("  sample lines:")
        for s in samples:
            print(f"     {s}")
    miss_mk = [m for m in markets if m not in markets_all]
    miss_bk = WANT_BOOKS - books_all
    if miss_bk:
        print(f"  ⚠ books not seen: {sorted(miss_bk)}")
    if miss_mk:
        print(f"  ⚠ markets not seen: {miss_mk}")
    ok = not miss_bk and not miss_mk
    if not markets_all:
        print("  (no props on the soonest games yet — likely posted closer to tip)")
        return None
    print(f"  → {label}: {'PASS ✅' if ok else 'partial'}")
    return ok


def main():
    print("The Odds API — FREE-TIER coverage check (live endpoints only; no historical spend)")
    m = check("baseball_mlb", MLB_MARKETS, "MLB pitcher outs")
    # probe WNBA with the core 4 (40 credits/event) to be economical on the free quota
    w = check("basketball_wnba", ["player_points", "player_rebounds", "player_assists",
                                  "player_points_rebounds_assists"], "WNBA player props")
    print("\n" + "=" * 60)
    print("VERDICT")
    print(f"  MLB  pitcher_outs + FD/DK : {'confirmed' if m else 'inconclusive — retry near a slate' if m is None else 'check above'}")
    print(f"  WNBA player props + FD/DK : {'confirmed' if w else 'inconclusive — retry near a slate' if w is None else 'check above'}")
    if m and w:
        print("\n  ✅ GO — both harnesses map to real posted markets. Safe to buy one month + fetch.")
    else:
        print("\n  Re-run when both sports have a live slate (props post a few hours before tip).")


if __name__ == "__main__":
    main()
