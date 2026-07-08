"""MLB game/F5/team totals — DIRECT soft-vs-sharp edges (no model, no anchor).

The safest kind of edge: Pinnacle and the soft books both post the SAME market
(game Total Runs, First-5-Innings Total Runs, each team's Total Runs), so we compare
line-for-line — Shin-devig Pinnacle for fair prob, take the soft price if it beats it.
No projection model means no model error (the trap that sank the batter alt-line
bucket). Settlement comes straight from the MLB linescore.

Pinnacle: sport 3, total (period 0 = game, period 1 = F5), team_total (period 0).
FanDuel/DK: 'Total Runs', 'First 5 Innings Total Runs', '<Team> Total Runs'.

Bets land in bet_ledger with stat in {game_total, f5_total, team_total_<home|away>}
and src='direct'. Run by the collect-odds workflow before bet_ledger.py.
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
import wnba_edge_scan as W                      # canon, fair_prob
import pinnacle as P                            # american_to_decimal


def _ekey(team_a, team_b):
    """Order-independent event key from two team names (last word = nickname, so
    'Minnesota Twins' and Pinnacle's 'Twins' both reduce to 'twins')."""
    return tuple(sorted(W.canon(t.split()[-1]) for t in (team_a, team_b) if t))

DB = Path(os.environ.get("MLB_TOTALS_DB", HERE / "mlb_totals.sqlite"))
AK = os.environ.get("FD_AK", "FhMFpcPWXMeyZxOx")
FD = f"https://sbapi.{os.environ.get('FD_STATE','ny')}.sportsbook.fanduel.com/api"
FD_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "application/json"}
PINN_H = {"x-api-key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R", "User-Agent": "Mozilla/5.0",
          "Accept": "application/json"}
MIN_EV, MAX_EV = 0.02, 0.30

DDL = """CREATE TABLE IF NOT EXISTS totals (
    collected_at TEXT, book TEXT, event TEXT, market TEXT, team TEXT, line REAL,
    over_odds REAL, under_odds REAL, start_time TEXT,
    PRIMARY KEY (collected_at, book, event, market, team, line))"""


# --------------------------------------------------------------------- Pinnacle side
def _pinn():
    """{(canon_event, market, team_side): (line, p_over, start)} from Pinnacle,
    market in {game_total, f5_total}, team_total keyed by side."""
    mu = requests.get("https://guest.api.arcadia.pinnacle.com/0.1/sports/3/matchups",
                      headers=PINN_H, timeout=30).json()
    mk = requests.get("https://guest.api.arcadia.pinnacle.com/0.1/sports/3/markets/straight",
                      headers=PINN_H, timeout=30).json()
    names, starts = {}, {}
    for m in mu:
        if m.get("type") != "matchup" or m.get("units") != "Regular":
            pass
        parts = m.get("participants") or []
        if len(parts) == 2 and all(p.get("name") for p in parts):
            names[m["id"]] = (parts[0]["name"], parts[1]["name"])
            starts[m["id"]] = m.get("startTime")
    # Pinnacle posts a MAIN line (isAlternate False) plus alternates at other numbers.
    # Index a fair prob at EVERY offered line so FD's exact posted line can be matched
    # line-for-line (that's the whole point of the direct method). Value:
    #   {(ekey, market, side): {line: (fair_over, start), ..., "main": line, "nm": names}}
    out = {}
    by_mid = {}
    for m in mk:
        by_mid.setdefault(m.get("matchupId"), []).append(m)
    for mid, nm in names.items():
        ev = _ekey(nm[0], nm[1])
        for m in by_mid.get(mid, []):
            per, typ = m.get("period"), m.get("type")
            if typ == "total" and per in (0, 1):
                key = (ev, "game_total" if per == 0 else "f5_total", "")
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
            if not m.get("isAlternate"):
                slot["main"] = round(float(pts), 1)
    return out, names


# --------------------------------------------------------------------- FanDuel side
def _fd_events():
    j = requests.get(f"{FD}/content-managed-page?page=CUSTOM&customPageId=mlb"
                     f"&timezone=America%2FNew_York&_ak={AK}", headers=FD_H, timeout=30).json()
    return {eid: e for eid, e in (j.get("attachments", {}).get("events", {}) or {}).items()
            if "@" in (e.get("name") or "")}


def _fd_totals(eid):
    """[(market, team, line, over, under)] for one FD event."""
    ev = requests.get(f"{FD}/event-page?eventId={eid}&tab=first-5-innings&_ak={AK}"
                      f"&timezone=America%2FNew_York", headers=FD_H, timeout=25).json()
    out = []
    for m in (ev.get("attachments", {}).get("markets", {}) or {}).values():
        nm = (m.get("marketName") or "").strip()
        low = nm.lower()
        if low == "total runs":
            market, team = "game_total", ""
        elif low == "first 5 innings total runs":
            market, team = "f5_total", ""
        elif low.endswith("total runs") and low not in ("total runs",):
            market, team = "team_total", nm[:-len(" Total Runs")].strip()
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


# --------------------------------------------------------------------- flag + store
def collect():
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    pinn, names = _pinn()
    con = sqlite3.connect(DB)
    con.execute(DDL)
    bets = []
    for eid, e in _fd_events().items():
        ename = e.get("name", "")
        # FD event name "Away (P) @ Home (P)" -> order-independent team key
        teams = [t.split("(")[0].strip() for t in ename.replace(" @ ", "@").split("@")]
        ev = _ekey(*teams) if len(teams) == 2 else ()
        start = e.get("openDate")
        for market, team, line, over, under in _fd_totals(eid):
            con.execute("INSERT OR IGNORE INTO totals VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts, "fd", ename, market, team, line, over, under, start))
            # match to Pinnacle: same market + EXACT line (main or alternate). team_total
            # picks the side by which team name the FD label contains.
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
            pfair = slot[L]                       # Pinnacle fair P(over) at FD's exact line
            for side, price, p in (("over", over, pfair), ("under", under, 1 - pfair)):
                ev_pct = p * price - 1
                if MIN_EV <= ev_pct <= MAX_EV:
                    stat = market if not side_key else f"team_total_{side_key}"
                    bets.append({"sport": "mlb", "event": ename,
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
    print(f"mlb_totals: {len(bets)} +EV found, {added} new logged")


if __name__ == "__main__":
    main()
