"""Standalone FanDuel collector (GitHub Actions, no device) — MLB pitcher + batter props
and tennis (games / set betting). FanDuel's sbapi is reachable from GitHub US IPs.

Flow per sport page (content-managed-page?customPageId=...):
  events -> per event: event-page -> layout.tabs -> event-page?tab=<slug> -> markets.

Market structures handled:
  * pitcher props   "{Pitcher} - [Alt ]Strikeouts/Pitching Outs"  (player in name; O/U or X+)
  * batter props    "To Record X+ Total Bases/Hits/Home Runs"      (players are runners; X+)
  * tennis          "Total Games Over/Under", "Set Betting"/"..."   (handicap line / correct score)

American -> decimal. Writes fd_lines(sport,event,player,stat,line,side,odds).
FRAGILE: FanDuel rotates _AK (env FD_AK) and tennis customPageId rotates by tournament
(env FD_TENNIS_PAGES, comma-separated). Refresh from the site when it stops returning data.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import urllib.request
from collections import Counter
from pathlib import Path

AK = os.environ.get("FD_AK", "FhMFpcPWXMeyZxOx")
STATE = os.environ.get("FD_STATE", "ny")
BASE = f"https://sbapi.{STATE}.sportsbook.fanduel.com/api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DB = Path(os.environ.get("FD_DB", Path(__file__).resolve().parent / "fanduel_props.sqlite"))
# try several page slugs — tournament pages rotate (wimbledon ends, us-open starts);
# unknown slugs error per-page and are skipped, so extras cost nothing and the generic
# ones keep tennis collection alive between slams without a manual env update
TENNIS_PAGES = os.environ.get(
    "FD_TENNIS_PAGES", "tennis,wimbledon,us-open,atp,wta").split(",")

PITCHER_STATS = {"strikeout": "strikeouts", "pitching outs": "outs", "outs recorded": "outs"}
# order matters: the combined market MUST precede "hits" — 'To Record 2+ Hits +
# Runs + RBIs' was substring-matching "hits" and polluting hits 1.5 with ~-210
# prices (H+R+RBI is likelier than hits alone, so its odds are always shorter).
BATTER_STATS = {"hits + runs + rbis": "hrr",
                "total bases": "total_bases", "hits": "hits", "home run": "home_runs",
                "rbi": "rbis", "stolen base": "stolen_bases"}
# order matters: combos before singles so "pts + reb" isn't caught by "points"/"rebounds"
WNBA_STATS = {"pts + reb + ast": "pra", "pts + ast": "pts_ast", "pts + reb": "pts_reb",
              "reb + ast": "reb_ast", "made threes": "threes", "points": "points",
              "rebounds": "rebounds", "assists": "assists"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=25))


def dec(a):
    if a is None:
        return None
    a = float(a)
    return round(1 + (a / 100 if a > 0 else 100 / -a), 4)


def _odds(r):
    return dec(((r.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {}).get("americanOdds"))


def extract(m, sport, event_name=""):
    """Yield (player, stat, line, side, odds) from one FanDuel market. event_name is
    the FD match name, used as the key for match-level markets (tennis match_games)."""
    nm = m.get("marketName") or ""
    low = nm.lower()
    rows = []
    # 0) WNBA player prop — two-sided "{Player} - Points" (Over/Under + handicap) or
    #    alt "To Score X+ Points" / "1+ Made Threes" (runners are players, over-only).
    if sport == "wnba":
        # FanDuel posts per-period markets ("1st Quarter Points", "1st Half Rebounds") whose names
        # substring-match "points"/"rebounds" and leak in as phantom low even-odds rungs that hijack
        # full-game main-line detection (a 20-pt scorer's "points" main line reading 5.5). Full-game
        # markets only — reject anything period-qualified before the WNBA_STATS substring match.
        if re.search(r"\b(quarter|half|period)\b", low):
            return rows
        wstat = next((v for k, v in WNBA_STATS.items() if k in low), None)
        if not wstat:
            return rows
        if " - " in nm:
            player = re.split(r"\s+-\s+", nm)[0].strip()
            for r in m.get("runners") or []:
                o, rn, h = _odds(r), (r.get("runnerName") or "").lower(), r.get("handicap")
                if o is None or h is None:
                    continue
                toks = rn.split()
                if toks and toks[-1] in ("over", "under"):
                    rows.append((player, wstat, float(h), toks[-1], o))
        elif (mm := re.search(r"(\d+)\+", nm)):
            line = int(mm.group(1)) - 0.5
            for r in m.get("runners") or []:
                o = _odds(r)
                if o is not None:
                    rows.append((r.get("runnerName") or "", wstat, line, "over", o))
        return rows
    # 1) pitcher prop: "{Pitcher} - ... Strikeouts/Outs"
    pstat = next((v for k, v in PITCHER_STATS.items() if k in low), None)
    if pstat and " - " in nm:
        player = re.split(r"\s+-\s+", nm)[0].strip()
        for r in m.get("runners") or []:
            o, rn = _odds(r), (r.get("runnerName") or "")
            if o is None:
                continue
            h = r.get("handicap")
            if rn.lower().startswith("over") and h is not None:
                rows.append((player, pstat, float(h), "over", o))
            elif rn.lower().startswith("under") and h is not None:
                rows.append((player, pstat, float(h), "under", o))
            elif (mm := re.search(r"(\d+)\+", rn)):
                rows.append((player, pstat, int(mm.group(1)) - 0.5, "over", o))
        return rows
    # 2) batter prop: "To Record X+ <Stat>" (players are runners)
    bstat = next((v for k, v in BATTER_STATS.items() if k in low), None)
    if bstat and (mm := re.search(r"(\d+)\+", nm)):
        line = int(mm.group(1)) - 0.5
        for r in m.get("runners") or []:
            o, rn = _odds(r), (r.get("runnerName") or "")
            if o is None or " & " in rn:     # 'A & B' = two-player combo market, not a prop
                continue
            rows.append((rn, bstat, line, "over", o))
        return rows
    # 3) tennis: player/team total games ("{Player} Total Games 19.5" — the line lives in
    #    the MARKET NAME, not the runner handicap), plus set betting / correct score.
    #    Per-set variants ("Set 2 Total Games ...") are exotics we skip.
    if sport == "tennis":
        # MATCH total games (2-way O/U) — the direct market vs Pinnacle's games line.
        # FD posts it as 'Alternative Total Games N X.5' (line at end) and a main
        # 'Total Games X.5' / 'Total Games Over/Under X.5'. Store as match_games with
        # the event name as the key (both players).
        mm = re.match(r"(?:alternative )?total games(?: \d+)?(?: over/under)? "
                      r"(\d+(?:\.\d+)?)$", low)
        if mm:
            ln = float(mm.group(1))
            for r in m.get("runners") or []:
                o, rn = _odds(r), (r.get("runnerName") or "").lower()
                if o is not None and rn.startswith(("over", "under")):
                    rows.append((event_name, "match_games", ln, rn.split()[0], o))
            return rows
        if (pm := re.match(r"(?P<pl>.+?)\s+Total Games\s+(?P<ln>\d+(?:\.\d+)?)$", nm)) \
                and not low.startswith("set"):
            pl, ln = pm.group("pl").strip(), float(pm.group("ln"))
            for r in m.get("runners") or []:
                o, rn = _odds(r), (r.get("runnerName") or "").lower()
                if o is not None and rn.startswith(("over", "under")):
                    rows.append((pl, "player_games", ln, rn.split()[0], o))
        elif "total games" in low or ("games" in low and "over" in low):
            for r in m.get("runners") or []:
                o, rn, h = _odds(r), (r.get("runnerName") or ""), r.get("handicap")
                if o is not None and h is not None and rn.lower().startswith(("over", "under")):
                    rows.append((nm, "total_games", float(h), rn.split()[0].lower(), o))
        elif "set betting" in low or "correct" in low and "set" in low:
            for r in m.get("runners") or []:
                o = _odds(r)
                if o is not None:
                    rows.append((r.get("runnerName") or "", "set_score", None, "yes", o))
    return rows


def collect_page(customPageId, sport, is_match):
    page = get(f"{BASE}/content-managed-page?page=CUSTOM&customPageId={customPageId}"
               f"&timezone=America%2FNew_York&_ak={AK}")
    evs = page.get("attachments", {}).get("events", {})
    out, seen = [], set()
    for eid, e in evs.items():
        nm = e.get("name") or ""
        if not is_match(nm):
            continue
        try:
            ev = get(f"{BASE}/event-page?eventId={eid}&_ak={AK}&timezone=America%2FNew_York")
        except Exception:
            continue
        tabs = ev.get("layout", {}).get("tabs", {})
        titles = [(t.get("title") if isinstance(t, dict) else str(t)) for t in tabs.values()]
        want = [t for t in titles if any(w in (t or "").lower()
                for w in ("popular", "player", "pitcher", "batter", "prop", "games", "set"))]
        for title in want or titles[:3]:
            slug = (title or "").lower().replace(" ", "-").replace("'", "")
            try:
                r = get(f"{BASE}/event-page?eventId={eid}&tab={slug}&_ak={AK}&timezone=America%2FNew_York")
            except Exception:
                continue
            for mid, m in (r.get("attachments", {}).get("markets", {}) or {}).items():
                if mid in seen:
                    continue
                seen.add(mid)
                for (pl, st, ln, sd, od) in extract(m, sport, nm):
                    out.append((sport, nm, pl, st, ln, sd, od))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--wnba", action="store_true", help="WNBA page ONLY — fast poll for the 60s loop")
    args = ap.parse_args()
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()
    rows = []
    if not args.wnba:
        try:
            rows += collect_page("mlb", "mlb", lambda n: "@" in n)
        except Exception as e:
            print("mlb page err:", str(e)[:80])
    try:
        rows += collect_page("wnba", "wnba", lambda n: "@" in n)
    except Exception as e:
        print("wnba page err:", str(e)[:80])
    if not args.wnba:
        for pg in TENNIS_PAGES:
            try:
                rows += collect_page(pg.strip(), "tennis", lambda n: " v " in n.lower())
            except Exception as e:
                print(f"tennis page {pg} err:", str(e)[:80])
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS fd_lines (
        collected_at TEXT, sport TEXT, event TEXT, player TEXT, stat TEXT, line REAL,
        side TEXT, odds REAL, PRIMARY KEY (collected_at, sport, player, stat, line, side))""")
    # explicit column list: dk_collect added a 'book' column (DEFAULT 'fd'), so a
    # positional 8-value insert crashes against the 9-column table
    con.executemany("INSERT OR REPLACE INTO fd_lines "
                    "(collected_at,sport,event,player,stat,line,side,odds) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    [(ts, *r) for r in rows])
    con.commit()
    con.close()
    by = Counter((r[0], r[3]) for r in rows)
    print(f"[{ts}] FanDuel {len(rows)} lines {dict(by)} -> {DB}", flush=True)


if __name__ == "__main__":
    main()
