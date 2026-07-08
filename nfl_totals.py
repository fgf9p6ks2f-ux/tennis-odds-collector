"""NFL game/team totals — DIRECT soft-vs-sharp, live now on Week-1 lines.

Same model-free method as mlb_totals: Pinnacle (sport 15, league NFL) and FanDuel
both post the game Total Match Points and each team's total, so we compare
line-for-line and take FD's price when it beats Pinnacle's devigged fair. Works in
the offseason because both books post early-season lines; player props (the big
September prize) auto-activate here once FanDuel posts them — the Pinnacle side
(127 prop specials already up) is wired, the FD side no-ops until props appear.

Settlement via ESPN's scoreboard (final team scores). Bets: nfl game_total /
team_total_{home,away}, src=direct.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wnba_edge_scan as W
import pinnacle as P
from mlb_totals import _ekey                    # order-independent team key

DB = Path(os.environ.get("NFL_TOTALS_DB", HERE / "nfl_totals.sqlite"))
AK = os.environ.get("FD_AK", "FhMFpcPWXMeyZxOx")
FD = f"https://sbapi.{os.environ.get('FD_STATE','ny')}.sportsbook.fanduel.com/api"
FD_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "application/json"}
PINN_H = {"x-api-key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R", "User-Agent": "Mozilla/5.0",
          "Accept": "application/json"}
MIN_EV, MAX_EV = 0.02, 0.30
NFL_SPORT = 15
LOG_WITHIN_DAYS = 3          # collect lines year-round, but only LOG bets this close to
                             # kickoff — offseason lines move too much to bet on now

DDL = """CREATE TABLE IF NOT EXISTS totals (
    collected_at TEXT, book TEXT, event TEXT, market TEXT, team TEXT, line REAL,
    over_odds REAL, under_odds REAL, start_time TEXT,
    PRIMARY KEY (collected_at, book, event, market, team, line))"""


def _pinn():
    mu = requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/sports/{NFL_SPORT}/matchups",
                      headers=PINN_H, timeout=30).json()
    mk = requests.get(f"https://guest.api.arcadia.pinnacle.com/0.1/sports/{NFL_SPORT}/markets/straight",
                      headers=PINN_H, timeout=30).json()
    names, starts = {}, {}
    for m in mu:
        if (m.get("league") or {}).get("name") != "NFL" or m.get("special"):
            continue
        parts = m.get("participants") or []
        if len(parts) == 2 and all(p.get("name") for p in parts):
            names[m["id"]] = (parts[0]["name"], parts[1]["name"])
            starts[m["id"]] = m.get("startTime")
    by_mid = {}
    for m in mk:
        by_mid.setdefault(m.get("matchupId"), []).append(m)
    out = {}
    for mid, nm in names.items():
        ev = _ekey(nm[0], nm[1])
        for m in by_mid.get(mid, []):
            per, typ = m.get("period"), m.get("type")
            if typ == "total" and per == 0:
                key = (ev, "game_total", "")
            elif typ == "team_total" and per == 0:
                key = (ev, "team_total", "home" if m.get("side") == "home" else "away")
            else:
                continue
            prices = {(p.get("designation"), p.get("points")): p.get("price")
                      for p in m.get("prices", [])}
            pts = next((p for (d, p) in prices if d == "over"), None)
            if pts is None:
                continue
            fo = W.fair_prob(P.american_to_decimal(prices.get(("over", pts))),
                             P.american_to_decimal(prices.get(("under", pts))))
            if fo is None:
                continue
            slot = out.setdefault(key, {"start": starts[mid], "nm": nm})
            slot[round(float(pts), 1)] = fo
    return out


def _fd_events():
    j = requests.get(f"{FD}/content-managed-page?page=CUSTOM&customPageId=nfl"
                     f"&timezone=America%2FNew_York&_ak={AK}", headers=FD_H, timeout=30).json()
    return {eid: e for eid, e in (j.get("attachments", {}).get("events", {}) or {}).items()
            if " @ " in (e.get("name") or "")}


def _fd_totals(eid):
    ev = requests.get(f"{FD}/event-page?eventId={eid}&_ak={AK}"
                      f"&timezone=America%2FNew_York", headers=FD_H, timeout=25).json()
    out = []
    for m in (ev.get("attachments", {}).get("markets", {}) or {}).values():
        nm = (m.get("marketName") or "").strip()
        low = nm.lower()
        if low == "total match points":
            market, team = "game_total", ""
        elif low.endswith("total match points") and low != "total match points":
            market, team = "team_total", nm[:-len(" Total Match Points")].strip()
        else:
            continue
        line = over = under = None
        for r in m.get("runners") or []:
            o = ((r.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {})
            dec = P.american_to_decimal(o.get("americanOdds"))
            rn = (r.get("runnerName") or "").lower()
            if r.get("handicap") is not None:
                line = float(r["handicap"])
            if "over" in rn:
                over = dec
            elif "under" in rn:
                under = dec
        if line is not None and over and under:
            out.append((market, team, line, over, under))
    return out


def collect():
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    pinn = _pinn()
    con = sqlite3.connect(DB)
    con.execute(DDL)
    bets = []
    for eid, e in _fd_events().items():
        ename = e.get("name", "")
        teams = [t.strip() for t in ename.split(" @ ")]
        ev = _ekey(*teams) if len(teams) == 2 else ()
        start = e.get("openDate")
        for market, team, line, over, under in _fd_totals(eid):
            con.execute("INSERT OR IGNORE INTO totals VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts, "fd", ename, market, team, line, over, under, start))
            side_key = ""
            if market == "team_total":
                pk = next((k for k in pinn if k[0] == ev and k[1] == "team_total"
                           and W.canon(team.split()[-1]) in
                           W.canon("".join(pinn[k]["nm"]))), None)
                if not pk:
                    continue
                side_key = pk[2]
            else:
                pk = (ev, market, "")
            slot = pinn.get(pk)
            L = round(float(line), 1)
            if not slot or L not in slot:
                continue
            try:
                soon = (dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                        - dt.datetime.now(dt.timezone.utc)).days <= LOG_WITHIN_DAYS
            except (ValueError, TypeError):
                soon = False
            pfair = slot[L]
            for side, price, p in (("over", over, pfair), ("under", under, 1 - pfair)):
                ev_pct = p * price - 1
                if soon and MIN_EV <= ev_pct <= MAX_EV:
                    stat = market if not side_key else f"team_total_{side_key}"
                    bets.append({"sport": "nfl", "event": ename,
                                 "player": f"{team or 'GAME'} ({market})",
                                 "stat": stat, "line": line, "side": side, "odds": price,
                                 "fair": p, "ev": ev_pct, "pinn_line": L,
                                 "start": start, "src": "direct", "book": "fd"})
    con.commit()
    con.close()
    return bets


def main():
    from bet_ledger import DDL as LDDL, LEDGER, bet_id, notify_ev, now
    bets = collect()
    con = sqlite3.connect(LEDGER)
    con.execute(LDDL)
    ts = now()
    added, new = 0, []
    for b in bets:
        bid = bet_id(b)
        if con.execute("SELECT 1 FROM bets WHERE bet_id=?", (bid,)).fetchone():
            continue
        con.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, ts, b["sport"], b["event"], b["player"], b["stat"], b["line"],
                     b["side"], b["odds"], 1.0, b["fair"], b["ev"] * 100, b["pinn_line"],
                     b["src"], b["start"], "open", None, None, None, None, None, None))
        added += 1
        new.append(b)
    con.commit()
    con.close()
    notify_ev(new)
    print(f"nfl_totals: {len(bets)} +EV found, {added} new logged")


if __name__ == "__main__":
    main()
