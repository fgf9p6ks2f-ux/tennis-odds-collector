"""H2H GG League (FanDuel esports) — results history + FD-line-aware bet flags.

The pieces:
  RESULTS  api-h2h.hudstats.com/v1/schedule/{fifa|nba|nfl}?date=...  (free, no key;
           only serves ±30 days, so gg.sqlite accrues the archive forward)
  LINES    FanDuel CO sbapi esports page -> event-page per match: the ACTUAL posted
           "Total Goals"/"Total Points" line + both prices, ~30-60 min before start
  FLAG     pair (nickname) history from gg.sqlite -> hit rate AT THE POSTED LINE;
           alert + log to the real bet ledger when rate >= the sport's tier

Every flag lands in bet_ledger.sqlite with FD's real price, so grading, CLV (vs our
own later FD quotes) and the nightly digest all work exactly like mlb/wnba/tennis.

    python gg_collect.py                # ingest 2 days + flag current FD board
    python gg_collect.py --backfill 30  # first run: pull the full available window
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import urllib.parse as up
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
GG_DB = HERE / "gg.sqlite"
LEDGER = HERE / "bet_ledger.sqlite"

HUD = "https://api-h2h.hudstats.com"
HUD_H = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                        "Safari/537.36"),
         "Accept": "application/json",
         "Origin": "https://h2hggl.com", "Referer": "https://h2hggl.com/"}

AK = os.environ.get("FD_AK", "FhMFpcPWXMeyZxOx")
FD = "https://sbapi.co.sportsbook.fanduel.com/api"     # CO catalog carries esports
FD_H = {"User-Agent": HUD_H["User-Agent"], "Accept": "application/json"}
ESPORTS_ETID = 27454571

# hudstats sport code -> ledger sport key, FD competition fragment, totals market
# fragment, alert tier. Tiers from the 2026-07-08 walk-forward validation on the
# 30-day window: esoccer 76.5% (z=+16.8) vs pair-median lines -> 0.70; ebasketball
# only 58-64% pair-aware -> stricter 0.75 and let the ledger judge; efootball
# under-sampled (28 deep pairs) -> 0.75. Re-tune as gg.sqlite deepens.
SPORTS_GG = {
    "fifa": {"sport": "esoccer", "comp": "eSoccer", "market": "total goals",
             "tier": 0.70, "min_n": 15, "tag": "EsocGG"},
    "nba":  {"sport": "ebasketball", "comp": "eBasketball", "market": "total points",
             "tier": 0.75, "min_n": 15, "tag": "EbbGG"},
    "nfl":  {"sport": "efootball", "comp": "eFootball", "market": "total points",
             "tier": 0.75, "min_n": 15, "tag": "EfbGG"},
}

try:
    from zoneinfo import ZoneInfo
    MT = ZoneInfo("America/Denver")
except Exception:
    MT = dt.timezone(dt.timedelta(hours=-6))


def mt_time(iso):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return t.astimezone(MT).strftime("%-I:%M%p MT")
    except ValueError:
        return "?"


# --------------------------------------------------------------------- results ingest
DDL = """CREATE TABLE IF NOT EXISTS gg_matches (
    external_id TEXT PRIMARY KEY, sport TEXT, start TEXT, p1 TEXT, p2 TEXT,
    s1 INTEGER, s2 INTEGER, total INTEGER);
CREATE TABLE IF NOT EXISTS gg_quotes (
    collected_at TEXT, sport TEXT, p1 TEXT, p2 TEXT, start TEXT, line REAL,
    over_odds REAL, under_odds REAL, PRIMARY KEY (collected_at, sport, p1, p2, line))"""


def ingest(days):
    con = sqlite3.connect(GG_DB)
    con.executescript(DDL)
    today = dt.datetime.now(dt.timezone.utc).date()
    added = 0
    for code in SPORTS_GG:
        for back in range(days):
            day = (today - dt.timedelta(days=back)).isoformat()
            q = up.quote(f"{day}T00:00:00+00:00")
            try:
                j = requests.get(f"{HUD}/v1/schedule/{code}?date={q}",
                                 headers=HUD_H, timeout=30).json()
            except (requests.RequestException, ValueError):
                continue
            rows = []
            for m in j if isinstance(j, list) else []:
                if m.get("matchStatus") != "MATCH_ENDED" or m.get("isCancelled"):
                    continue
                a, b = m.get("teamAScore"), m.get("teamBScore")
                pa, pb = m.get("participantAName"), m.get("participantBName")
                if a is None or b is None or not pa or not pb:
                    continue
                rows.append((m.get("externalId"), code, m.get("startDate"),
                             pa.upper(), pb.upper(), a, b, a + b))
            before = con.total_changes
            con.executemany("INSERT OR IGNORE INTO gg_matches VALUES (?,?,?,?,?,?,?,?)",
                            rows)
            con.commit()
            added += con.total_changes - before
    con.close()
    return added


def histories(code):
    """{frozenset(nickA, nickB): [(start, total)] chronological}."""
    con = sqlite3.connect(GG_DB)
    con.executescript(DDL)
    rows = con.execute("SELECT start, p1, p2, total FROM gg_matches WHERE sport=? "
                       "ORDER BY start", (code,)).fetchall()
    con.close()
    h = {}
    for start, a, b, tot in rows:
        h.setdefault(frozenset((a, b)), []).append((start, tot))
    return h


# --------------------------------------------------------------------- FanDuel lines
def _american_to_dec(a):
    if a is None:
        return None
    a = float(a)
    return round(1 + (a / 100 if a > 0 else 100 / -a), 4)


NICKS = re.compile(r"\(([^)]+)\)")


def fd_board():
    """Upcoming GG events with their posted totals:
    [{code, nicks, start, event_name, line, over_odds, under_odds, eid}]."""
    j = requests.get(f"{FD}/content-managed-page?page=SPORT&eventTypeId={ESPORTS_ETID}"
                     f"&timezone=America%2FDenver&_ak={AK}", headers=FD_H, timeout=30).json()
    at = j.get("attachments", {})
    comps = {cid: c.get("name", "") for cid, c in (at.get("competitions") or {}).items()}
    out = []
    for eid, e in (at.get("events") or {}).items():
        comp = comps.get(str(e.get("competitionId")), "")
        code = next((c for c, cfg in SPORTS_GG.items() if cfg["comp"].lower()
                     in comp.lower()), None)
        nicks = [n.upper() for n in NICKS.findall(e.get("name", ""))]
        if not code or len(nicks) != 2:
            continue
        try:
            ev = requests.get(f"{FD}/event-page?eventId={eid}&_ak={AK}"
                              f"&timezone=America%2FDenver", headers=FD_H, timeout=25).json()
        except (requests.RequestException, ValueError):
            continue
        want = SPORTS_GG[code]["market"]
        for m in (ev.get("attachments", {}).get("markets", {}) or {}).values():
            # EXACT market name only — 'total points' must not match '1st Quarter
            # Total Points' (a quarter line vs match history = garbage flags)
            if (m.get("marketName") or "").strip().lower() != want:
                continue
            line = over = under = None
            for r in m.get("runners") or []:
                o = ((r.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {})
                px = _american_to_dec(o.get("americanOdds"))
                nm = (r.get("runnerName") or "").lower()
                if r.get("handicap") is not None:
                    line = float(r["handicap"])
                if nm.startswith("over"):
                    over = px
                elif nm.startswith("under"):
                    under = px
            if line and over and under:
                out.append({"code": code, "nicks": nicks, "start": e.get("openDate"),
                            "event_name": e.get("name", ""), "line": line,
                            "over": over, "under": under, "eid": eid})
                break
    return out


# --------------------------------------------------------------------- flag + ledger
def flag_and_log():
    hists = {code: histories(code) for code in SPORTS_GG}
    board = fd_board()
    con = sqlite3.connect(GG_DB)
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    for b in board:                                     # quote snapshots (CLV later)
        con.execute("INSERT OR IGNORE INTO gg_quotes VALUES (?,?,?,?,?,?,?,?)",
                    (ts, b["code"], b["nicks"][0], b["nicks"][1], b["start"],
                     b["line"], b["over"], b["under"]))
    con.commit()
    con.close()

    led = sqlite3.connect(LEDGER)
    import bet_ledger as BL
    led.execute(BL.DDL)
    alerts, logged = [], 0
    for b in board:
        cfg = SPORTS_GG[b["code"]]
        h = hists[b["code"]].get(frozenset(b["nicks"]), [])
        n = len(h)
        if n < cfg["min_n"]:
            continue
        totals = sorted(t for _, t in h)
        # sanity band: the posted line must live inside the pair's own outcome range
        # (guards against alt lines / period markets / any parse surprise)
        if not (totals[max(0, n // 20)] - 1 <= b["line"] <= totals[min(n - 1, n - 1 - n // 20)] + 1):
            continue
        overs = sum(1 for _, t in h if t > b["line"])
        po = overs / n
        side, rate = ("over", po) if po >= 0.5 else ("under", 1 - po)
        if rate < cfg["tier"]:
            continue
        odds = b["over"] if side == "over" else b["under"]
        ev = rate * odds - 1
        if ev <= 0:                                     # price too bad even at our rate
            continue
        player = " v ".join(b["nicks"])
        bid = f"{cfg['sport']}|{BL.W.canon(player)}|total|{b['line']}|{side}|{str(b['start'])[:10]}"
        if led.execute("SELECT 1 FROM bets WHERE bet_id=?", (bid,)).fetchone():
            continue
        led.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bid, ts, cfg["sport"], b["event_name"], player, "total",
                     b["line"], side, odds, 1.0, rate, ev * 100, b["line"], "h2h",
                     b["start"], "open", None, None, None, None, None, None))
        logged += 1
        w = overs if side == "over" else n - overs
        am = round((odds - 1) * 100) if odds >= 2 else round(-100 / (odds - 1))
        try:
            start_ts = int(dt.datetime.fromisoformat(
                str(b["start"]).replace("Z", "+00:00")).timestamp())
        except ValueError:
            start_ts = 0
        alerts.append({"msg": (f"[{cfg['tag']}] {b['nicks'][0]} v {b['nicks'][1]} · "
                               f"{side[0].upper()}{b['line']:g} {am:+d} · "
                               f"{w}-{n-w} ({rate*100:.0f}%)"),
                       "start_ts": start_ts})
    led.commit()
    led.close()
    return board, alerts, logged


def notify(alerts):
    """One message PER GAME, delivered 5 minutes before ITS start (ntfy scheduled
    delivery via the At: header) — no bulk batches. A game already inside the 5-min
    window sends immediately. The line shown was FanDuel's posted number at flag
    time (~30-60 min out); confirm it hasn't moved when you bet."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not alerts:
        return
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    for a in alerts:
        at = a["start_ts"] - 300
        hdrs = {"Priority": "high", "Tags": "joystick"}
        if at > now + 30:
            hdrs["Title"] = "Esports bet — starts in 5 min"
            hdrs["At"] = str(at)
        else:
            hdrs["Title"] = "Esports bet — starting now"
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=a["msg"].encode(),
                          headers=hdrs, timeout=15)
        except requests.RequestException:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=None,
                    help="days of history to pull (first run: 30)")
    ap.add_argument("--no-flags", action="store_true", help="ingest only")
    args = ap.parse_args()
    con = sqlite3.connect(GG_DB)
    con.executescript(DDL)
    have = con.execute("SELECT COUNT(*) FROM gg_matches").fetchone()[0]
    con.close()
    days = args.backfill if args.backfill else (30 if have == 0 else 2)
    added = ingest(days)
    print(f"gg ingest: +{added} results ({days}d window)")
    if args.no_flags:
        return
    try:
        board, alerts, logged = flag_and_log()
        print(f"FD board: {len(board)} priced events · {logged} new bets logged")
        for a in alerts[:12]:
            print("  " + a["msg"])
        notify(alerts)
    except Exception as e:
        print(f"FD flag pass skipped: {e}")


if __name__ == "__main__":
    main()
