"""NBA port · flag-time engine — injuries × WOWY priors × live lines -> paper flags.

The live heart of the October system, runnable (and idling) today:

  1. nba_injuries.json (ESPN feed, every cycle)  ->  players with status OUT whose
     injury-report date is <= NEWS_DAYS old (the validated absence game-1/2 news window).
  2. nba_wowy.sqlite pair priors (team-scoped name match — the RotoWire collision
     lesson)  ->  beneficiaries B with proj_stat = rate_stat x (base_min + d_min).
  3. Freshest NBA lines in fanduel_props.sqlite (fd + dk, main two-sided lines, best
     over price — line shopping)  +  today's slate from cdn.nba.com (B's team must play
     today; CDN works from CI, unlike stats.nba).
  4. Flag OVERS ONLY (the three-system doctrine) on PRA / PTS / REB where
     proj - line >= the stat's validated margin. AST (collapses early-season, 48.5%
     Oct-Nov) and FG3M (~coinflip) are GATED OFF. Paper bets land in bet_ledger at the
     real best price; bet_id dedupes; settle_nba grades from ESPN box scores.

Offseason: 0 games on the scoreboard + 0 posted props -> clean no-op every cycle.

    python nba_flags.py             # flag + log paper bets
    python nba_flags.py --dry-run   # print flags, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
INJ = HERE / "nba_injuries.json"
WOWY = HERE / "nba_wowy.sqlite"
LINES_DB = HERE / "fanduel_props.sqlite"
LEDGER = HERE / "bet_ledger.sqlite"

NEWS_DAYS = 2                          # validated cell: absence game 1-2 (news window)
STATS = ("pra", "pts", "reb")          # ast/fg3m gated off (48.5% Oct-Nov / 53.7%)
MARGIN = {"pra": 5.0, "pts": 3.0, "reb": 2.0}
LEDGER_STAT = {"pra": "pra", "pts": "points", "reb": "rebounds"}   # fd_lines stat keys
LINE_MAX_AGE_MIN = 90                  # freshest snapshot must be this recent
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# ESPN team abbrevs (injury feed) -> NBA tricodes (wowy tables / cdn scoreboard)
TEAM_FIX = {"GS": "GSW", "SA": "SAS", "NO": "NOP", "NY": "NYK", "UTAH": "UTA",
            "WSH": "WAS", "PHX": "PHX", "CHA": "CHA"}


def fold(s):
    """ascii-fold + lower + strip punctuation — 'Kristaps Porziņģis' == 'Kristaps Porzingis'."""
    n = unicodedata.normalize("NFKD", s or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", "", n.lower()).split())


def outs_in_window(now):
    """{fold(name): (name, tricode)} for OUT players whose report date is fresh (news window)."""
    if not INJ.exists():
        return {}
    try:
        d = json.loads(INJ.read_text())
    except (ValueError, OSError):
        return {}
    out = {}
    for name, v in (d.get("injuries") or {}).items():
        if (v.get("status") or "").lower() != "out":
            continue
        try:
            age = (now.date() - dt.date.fromisoformat(v.get("date") or "")).days
        except ValueError:
            continue
        if 0 <= age <= NEWS_DAYS:
            team = (v.get("team") or "").upper()
            out[fold(name)] = (name, TEAM_FIX.get(team, team))
    return out


def todays_games():
    """{tricode: (game_id, game_time_utc, matchup)} from cdn.nba.com (CI-friendly)."""
    try:
        j = requests.get("https://cdn.nba.com/static/json/liveData/scoreboard/"
                         "todaysScoreboard_00.json", headers=UA, timeout=20).json()
    except (requests.RequestException, ValueError):
        return {}
    games = {}
    for g in (j.get("scoreboard") or {}).get("games", []):
        h = (g.get("homeTeam") or {}).get("teamTricode")
        a = (g.get("awayTeam") or {}).get("teamTricode")
        if not h or not a:
            continue
        mu = f"{a} @ {h}"
        for t in (h, a):
            games[t] = (g.get("gameId"), g.get("gameTimeUTC"), mu)
    return games


def latest_lines(now):
    """{(fold(player), stat): (line, best_over_dec, snapshot_ts)} — main two-sided lines
    from the freshest recent snapshot, best over price across fd+dk."""
    if not LINES_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{LINES_DB}?mode=ro", uri=True)
    ts = con.execute("SELECT MAX(collected_at) FROM fd_lines WHERE sport='nba'").fetchone()[0]
    if not ts:
        con.close()
        return {}
    try:
        age = (now.replace(tzinfo=None) - dt.datetime.fromisoformat(ts)).total_seconds() / 60
    except ValueError:
        age = 1e9
    if age > LINE_MAX_AGE_MIN:
        con.close()
        return {}
    rows = con.execute(
        "SELECT player, stat, line, side, odds, COALESCE(book,'fd') FROM fd_lines "
        "WHERE sport='nba' AND collected_at=?", (ts,)).fetchall()
    con.close()
    two = {}
    for player, stat, line, side, odds, book in rows:
        two.setdefault((fold(player), stat, line), {}).setdefault(side, []).append(odds)
    out = {}
    for (pf, stat, line), sides in two.items():
        if "over" not in sides or "under" not in sides:      # alt X+ rungs are over-only
            continue
        best_over = max(sides["over"])
        cur = out.get((pf, stat))
        # several two-sided lines (alts): keep the one priced nearest even = the main
        if cur is None or abs(best_over - 1.9091) < abs(cur[1] - 1.9091):
            out[(pf, stat)] = (line, best_over, ts)
    return out


def flags(dry=False):
    now = dt.datetime.now(dt.timezone.utc)
    outs = outs_in_window(now)
    if not outs:
        print("nba flags: 0 fresh OUTs — no-op")
        return []
    games = todays_games()
    lines = latest_lines(now)
    con = sqlite3.connect(f"file:{WOWY}?mode=ro", uri=True)
    pairs = con.execute("SELECT team, x_name, b_name, n_out, d_min, rate_pts, rate_reb, "
                        "rate_pra FROM pairs").fetchall()
    base = {fold(r[1]): (r[4], {"pts": r[5], "reb": r[6], "ast": r[7], "fg3m": r[8],
                                "pra": r[9]})
            for r in con.execute("SELECT b_id, b_name, team, last_date, base_min, med_pts,"
                                 " med_reb, med_ast, med_fg3m, med_pra FROM baselines")}
    con.close()
    rate_ix = {"pts": 5, "reb": 6, "pra": 7}
    out_bets = []
    for team, x_name, b_name, n_out, d_min, r_pts, r_reb, r_pra in pairs:
        xf = fold(x_name)
        if xf not in outs or outs[xf][1] != team:            # team-scoped name match
            continue
        if team not in games:                                 # beneficiary must play today
            continue
        bf = fold(b_name)
        if bf in outs:                                        # beneficiary himself is out
            continue
        bl = base.get(bf)
        if not bl:
            continue
        base_min = bl[0]
        rates = {"pts": r_pts, "reb": r_reb, "pra": r_pra}
        gid, gtime, matchup = games[team]
        for stat in STATS:
            proj = rates[stat] * (base_min + d_min)
            lk = lines.get((bf, LEDGER_STAT[stat]))
            if not lk:
                continue
            line, odds, ts = lk
            if proj - line < MARGIN[stat]:
                continue
            out_bets.append({"team": team, "x": x_name, "b": b_name, "stat": stat,
                             "line": line, "odds": odds, "proj": round(proj, 1),
                             "gid": gid, "gtime": gtime, "matchup": matchup})
    if not out_bets:
        print(f"nba flags: {len(outs)} fresh OUT(s), 0 flags "
              f"({len(games)} games today, {len(lines)} priced player-stats)")
        return []
    logged = 0
    if not dry:
        led = sqlite3.connect(LEDGER)
        import bet_ledger as BL
        led.execute(BL.DDL)
        ts_now = now.replace(microsecond=0, tzinfo=None).isoformat()
        for b in out_bets:
            bid = (f"nba|{fold(b['b'])}|{b['stat']}|{b['line']}|over|"
                   f"{str(b['gtime'])[:10]}")
            if led.execute("SELECT 1 FROM bets WHERE bet_id=?", (bid,)).fetchone():
                continue
            led.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (bid, ts_now, "nba", b["matchup"], b["b"], LEDGER_STAT[b["stat"]],
                         b["line"], "over", b["odds"], 1.0, None, None, b["line"], "wowy",
                         b["gtime"], "open", None, None, None, None, None, None))
            logged += 1
        led.commit()
        led.close()
    for b in out_bets:
        print(f"  [NBA] {b['b']} ({b['team']}) {b['stat'].upper()} O{b['line']:g} "
              f"@{b['odds']:.2f} · proj {b['proj']} · {b['x']} OUT · {b['matchup']}")
    print(f"nba flags: {len(out_bets)} flag(s), {logged} new paper bet(s) logged"
          + (" (dry-run)" if dry else ""))
    return out_bets


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    flags(dry=a.dry_run)
