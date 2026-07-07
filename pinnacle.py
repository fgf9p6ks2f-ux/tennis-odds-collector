"""Pinnacle odds collector (public guest JSON feed).

Endpoints (guest.api.arcadia.pinnacle.com), sport 33 = tennis:
  /0.1/sports/33/matchups          -> events (Sets-units main + Games-units child)
  /0.1/sports/33/markets/straight  -> ALL prices in one call (moneyline/spread/total)

A match appears as a "Sets" matchup (match winner, set spread ±1.5, set total o/u 2.5,
first-set lines) plus a linked "Games" matchup (parentId -> sets id) carrying the
total-games and games-spread lines. We merge them into one normalized record.

Prices are American; we convert to decimal. Personal-research use; rate-limit politely.
"""
from __future__ import annotations

import time

import requests

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"          # public guest key from pinnacle.com
HEADERS = {"x-api-key": KEY, "User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TENNIS = 33


def american_to_decimal(a) -> float | None:
    if a is None:
        return None
    a = float(a)
    return round(1 + (a / 100 if a > 0 else 100 / -a), 4)


def _get(path: str):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Pinnacle GET failed: {path}")


def fetch():
    return _get(f"/sports/{TENNIS}/matchups"), _get(f"/sports/{TENNIS}/markets/straight")


def _is_doubles(names) -> bool:
    return any("/" in n for n in names)


def _prices(market):
    return {(p.get("designation"), p.get("points")): p.get("price")
            for p in market.get("prices", [])}


def _pick_total(markets, want_points=None):
    """From a list of total markets pick the target line (by points) or the most
    balanced (main) line."""
    tots = [m for m in markets if m.get("type") == "total" and m.get("period") == 0]
    if not tots:
        return None
    if want_points is not None:
        for m in tots:
            pr = _prices(m)
            pts = {p for (_d, p) in pr}
            if want_points in pts:
                return m
    # else main line = smallest gap between over/under decimal odds
    def gap(m):
        pr = list(m.get("prices", []))
        if len(pr) < 2:
            return 9e9
        ds = [american_to_decimal(p["price"]) or 9e9 for p in pr]
        return abs(ds[0] - ds[1])
    return min(tots, key=gap)


def _pick_spread(markets, want_abs=None):
    sprs = [m for m in markets if m.get("type") == "spread" and m.get("period") == 0]
    if not sprs:
        return None
    if want_abs is not None:
        for m in sprs:
            if any(abs(p.get("points", 0)) == want_abs for p in m.get("prices", [])):
                return m
    def gap(m):
        ds = [american_to_decimal(p["price"]) or 9e9 for p in m.get("prices", [])[:2]]
        return abs(ds[0] - ds[1]) if len(ds) == 2 else 9e9
    return min(sprs, key=gap)


def normalize(matchups, markets) -> list[dict]:
    by_mid: dict[int, list] = {}
    for m in markets:
        by_mid.setdefault(m.get("matchupId"), []).append(m)

    # index Games child matchups by their parent (Sets) id
    games_child = {mu["parentId"]: mu for mu in matchups
                   if mu.get("units") == "Games" and mu.get("parentId")}

    out = []
    for mu in matchups:
        if mu.get("units") != "Sets" or mu.get("type") != "matchup" or mu.get("special"):
            continue
        parts = mu.get("participants", [])
        names = [p.get("name") for p in parts]
        if len(names) != 2 or None in names or _is_doubles(names):
            continue
        mks = by_mid.get(mu["id"], [])
        ml = next((m for m in mks if m.get("type") == "moneyline" and m.get("period") == 0), None)
        if not ml:
            continue
        mlp = _prices(ml)
        rec = {
            "match_id": mu["id"], "start_time": mu.get("startTime"),
            "league": (mu.get("league") or {}).get("name"),
            "best_of": mu.get("bestOfX"), "p1": names[0], "p2": names[1],
            "ml1": american_to_decimal(mlp.get(("home", None))),
            "ml2": american_to_decimal(mlp.get(("away", None))),
        }
        st = _pick_total(mks, want_points=2.5)          # set total (decider market)
        if st:
            p = _prices(st)
            pts = next((pp for (_d, pp) in p), 2.5)
            rec.update(set_total_line=pts,
                       set_over=american_to_decimal(p.get(("over", pts))),
                       set_under=american_to_decimal(p.get(("under", pts))))
        ss = _pick_spread(mks, want_abs=1.5)            # set spread (straight-sets)
        if ss:
            p = _prices(ss)
            rec.update(set_spread=1.5,
                       spr_home=american_to_decimal(p.get(("home", -1.5)) or p.get(("home", 1.5))),
                       spr_away=american_to_decimal(p.get(("away", 1.5)) or p.get(("away", -1.5))))
        gchild = games_child.get(mu["id"])              # linked games matchup
        if gchild:
            gm = _pick_total(by_mid.get(gchild["id"], []))
            if gm:
                p = _prices(gm)
                pts = next((pp for (_d, pp) in p), None)
                rec.update(games_line=pts,
                           games_over=american_to_decimal(p.get(("over", pts))),
                           games_under=american_to_decimal(p.get(("under", pts))))
        out.append(rec)
    return out


def collect() -> list[dict]:
    matchups, markets = fetch()
    return normalize(matchups, markets)
