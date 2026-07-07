"""Autonomous bet ledger — the results/CLV/P&L engine (runs every collection cycle).

One append-only sqlite table (`bets`) is the single source of truth. Each cycle it:
  1. FLAG+LOG  — find +EV FanDuel-vs-Pinnacle bets on the latest pre-game snapshot; log
                 each NEW one as a flat 1-unit ($100) bet at the price seen (idempotent by
                 bet_id, so a bet is recorded once — when the edge first appears).
  2. CLOSE     — once a bet's game has started, capture Pinnacle's CLOSING fair prob for
                 that line and compute CLV (did our taken price beat the sharp close?).
  3. GRADE     — once the game is final, pull the box score, mark W/L/push, book the P&L.
  4. REPORT    — write bet_ledger_report.md: W-L, units, $ P&L, ROI, avg CLV, by sport/stat.

CLV is the metric that matters (W/L over dozens of bets is noise); +EV bets should show
positive average CLV long before they show profit. Reuses wnba_edge_scan's math.

Deps: stdlib + requests (settlement only). Unsettleable bets just stay open and retry.
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wnba_edge_scan as W  # canon, fair_prob, prob_over, anchor, SD_CURVES

LEDGER = HERE / "bet_ledger.sqlite"
REPORT = HERE / "bet_ledger_report.md"
FD_DB = HERE / "fanduel_props.sqlite"
UNIT_USD = 100.0
MIN_EV = 0.02
MAX_EV = 0.30               # a real edge vs a sharp de-vig is small; >this = artifact, skip
MODEL_BAND = (0.20, 0.90)   # only model-price alt lines in this fair-prob band (not deep tails)
FINAL_BUFFER_H = 4.0        # a game is assumed final this many hours after first pitch/tip

SPORTS = {
    "mlb":  {"db": HERE / "mlb_kprops.sqlite", "table": "pitcher_props", "pcol": "pitcher",
             "model": True},
    "wnba": {"db": HERE / "wnba_props.sqlite", "table": "wnba_props", "pcol": "player",
             "model": True},
}


# ----------------------------------------------------------------------------- snapshots
def _rows(db, table, where=""):
    if not db.exists():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute(f"SELECT * FROM {table}{where}").fetchall()
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        rows, cols = [], []
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def _latest(rows):
    if not rows:
        return None, []
    mx = max(r["collected_at"] for r in rows)
    return mx, [r for r in rows if r["collected_at"] == mx]


def _pre(ts, snap):
    return str(ts)[:19] > str(snap)[:19]


# ----------------------------------------------------------------------------- flagging
def _benched():
    """(sport, stat) buckets the learner has benched for negative realized CLV."""
    import json
    f = HERE / "bet_filters.json"
    if not f.exists():
        return set()
    try:
        return {tuple(x) for x in json.loads(f.read_text()).get("benched", [])}
    except (ValueError, OSError):
        return set()


def _mkbet(sport, r, p_side, pinn_line, start, src):
    return {"sport": sport, "event": r["event"], "player": r["player"], "stat": r["stat"],
            "line": float(r["line"]), "side": r["side"], "odds": float(r["odds"]),
            "fair": p_side, "ev": p_side * float(r["odds"]) - 1, "pinn_line": pinn_line,
            "start": start, "src": src}


def flag(sport):
    """Return [dict] of +EV bets on the latest pre-game snapshot for `sport`."""
    cfg = SPORTS[sport]
    benched = _benched()
    psnap, plive = _latest(_rows(cfg["db"], cfg["table"]))
    if not plive:
        return []
    plive = [r for r in plive if r.get("start_time") and _pre(r["start_time"], psnap)]
    _, fd = _latest(_rows(FD_DB, "fd_lines", f" WHERE sport='{sport}'"))
    if not (plive and fd):
        return []
    pc = cfg["pcol"]
    ref, mains = {}, {}          # exact-line fair, and each player/stat's single main line
    for r in plive:
        p = W.fair_prob(r["over_odds"], r["under_odds"])
        if p is None:
            continue
        L = round(float(r["line"]), 1)
        ref[(W.canon(r[pc]), r["stat"], L)] = (p, float(r["line"]), r["start_time"])
        mains[(W.canon(r[pc]), r["stat"])] = (L, p, r["start_time"])
    bets = []
    # 1) direct — FanDuel line == a Pinnacle line (model-free, sharpest)
    for r in fd:
        L = round(float(r["line"]), 1) if r["line"] is not None else None
        hit = ref.get((W.canon(r["player"]), r["stat"], L))
        if hit:
            p, line0, start = hit
            bets.append(_mkbet(sport, r, p if r["side"] == "over" else 1 - p, line0, start, "direct"))
    # 2) model-priced alt overs Pinnacle doesn't post (sport-specific)
    if cfg.get("model"):
        bets += _model_alts(sport, mains, fd, ref)
    return [b for b in bets if MIN_EV <= b["ev"] <= MAX_EV
            and (sport, b["stat"]) not in benched]


def _model_alts(sport, mains, fd, ref):
    """Price FanDuel alt OVER lines Pinnacle doesn't post, by anchoring the sport's model
    to Pinnacle's main line for that player/stat."""
    bets = []
    if sport == "wnba":
        for r in fd:
            L = round(float(r["line"]), 1) if r["line"] is not None else None
            if (r["side"] != "over" or r["stat"] not in W.SD_CURVES
                    or (W.canon(r["player"]), r["stat"], L) in ref):
                continue
            m = mains.get((W.canon(r["player"]), r["stat"]))
            if not m:
                continue
            line0, p0, start = m
            mean, sd = W.anchor(line0, p0, r["stat"])
            p = W.prob_over(mean, L, sd)
            if MODEL_BAND[0] <= p <= MODEL_BAND[1]:
                bets.append(_mkbet(sport, r, p, line0, start, "model"))
    elif sport == "mlb":
        cand = [r for r in fd if r["side"] == "over" and r["stat"] == "total_bases"
                and (W.canon(r["player"]), "total_bases", round(float(r["line"]), 1)) not in ref
                and (W.canon(r["player"]), "total_bases") in mains]
        if not cand:
            return bets
        from mlb import batter, data
        try:
            season = int(str(next(iter(mains.values()))[2])[:4])
            lines = {W.canon(v["name"]): v for v in
                     data.all_batter_lines(season, min_pa=30).values() if v.get("name")}
        except Exception:
            return bets
        proj_cache = {}
        for r in cand:
            pk = W.canon(r["player"])
            bl = lines.get(pk)
            if not bl:
                continue
            line0, p0, start = mains[(pk, "total_bases")]
            if pk not in proj_cache:
                try:
                    proj_cache[pk] = batter.anchor(bl, line0, p0, exp_pa=batter.exp_pa_from(bl))
                except Exception:
                    proj_cache[pk] = None
            if proj_cache[pk] is None:
                continue
            L = round(float(r["line"]), 1)
            p = batter.prob_over(proj_cache[pk], L)
            if MODEL_BAND[0] <= p <= MODEL_BAND[1]:
                bets.append(_mkbet(sport, r, p, line0, start, "model"))
    return bets


def bet_id(b):
    day = str(b["start"])[:10]
    return f"{b['sport']}|{W.canon(b['player'])}|{b['stat']}|{b['line']}|{b['side']}|{day}"


# ----------------------------------------------------------------------------- closing / CLV
def closing_fair(sport, player, stat, line, start):
    """Pinnacle's de-vigged fair prob for this exact line at the last pre-start snapshot;
    for model stats, anchor the closing MAIN line to the alt line. None if unavailable."""
    cfg = SPORTS[sport]
    pc = cfg["pcol"]
    rows = [r for r in _rows(cfg["db"], cfg["table"])
            if W.canon(r[pc]) == W.canon(player) and r["stat"] == stat
            and r.get("start_time") and str(r["collected_at"])[:19] <= str(start)[:19]]
    if not rows:
        return None
    close_ts = max(r["collected_at"] for r in rows)
    snap = [r for r in rows if r["collected_at"] == close_ts]
    exact = [r for r in snap if round(float(r["line"]), 1) == round(float(line), 1)]
    if exact:
        return W.fair_prob(exact[0]["over_odds"], exact[0]["under_odds"])
    if cfg["model"] and stat in W.SD_CURVES:           # anchor the closing main line
        r = snap[0]
        p0 = W.fair_prob(r["over_odds"], r["under_odds"])
        if p0 is None:
            return None
        mean, sd = W.anchor(round(float(r["line"]), 1), p0, stat)
        return W.prob_over(mean, round(float(line), 1), sd)
    return None


# ----------------------------------------------------------------------------- settlement
MLB_FIELD = {"strikeouts": ("pitching", "strikeOuts"), "outs": ("pitching", "outs"),
             "hits_allowed": ("pitching", "hits"), "earned_runs": ("pitching", "earnedRuns"),
             "total_bases": ("hitting", "totalBases"), "hits": ("hitting", "hits"),
             "home_runs": ("hitting", "homeRuns"), "rbis": ("hitting", "rbi"),
             "stolen_bases": ("hitting", "stolenBases")}
NBA_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         "Referer": "https://www.wnba.com/", "Origin": "https://www.wnba.com",
         "x-nba-stats-origin": "stats", "x-nba-stats-token": "true",
         "Accept": "application/json, text/plain, */*"}


def _game_date(start):
    """US game date for a UTC start_time (evening games are next-day UTC)."""
    t = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    return (t - dt.timedelta(hours=4)).date().isoformat()      # EDT/CDT summer

def settle_mlb(player, stat, start):
    spec = MLB_FIELD.get(stat)
    if not spec:
        return None
    grp, fld = spec
    date = _game_date(start)
    try:
        s = requests.get("https://statsapi.mlb.com/api/v1/people/search",
                         params={"names": player}, timeout=20).json().get("people") or []
        if not s:
            return None
        g = requests.get(f"https://statsapi.mlb.com/api/v1/people/{s[0]['id']}/stats",
                         params={"stats": "gameLog", "group": grp, "season": date[:4]},
                         timeout=20).json()
        for sp in (g.get("stats") or [{}])[0].get("splits", []):
            if sp.get("date") == date:
                return float(sp["stat"].get(fld, 0) or 0)
    except (requests.RequestException, KeyError, ValueError):
        pass
    return None


def _wnba_combo(st, stat):
    p, r, a = st.get("PTS", 0), st.get("REB", 0), st.get("AST", 0)
    return {"points": p, "rebounds": r, "assists": a, "threes": st.get("FG3M", 0),
            "pra": p + r + a, "pts_reb": p + r, "pts_ast": p + a, "reb_ast": r + a}.get(stat)


def settle_wnba(player, stat, start):
    date = _game_date(start)
    season = date[:4]
    try:
        j = requests.get("https://stats.nba.com/stats/leaguedashplayerstats",
                         params={"Season": season, "SeasonType": "Regular Season",
                                 "LeagueID": "10", "PerMode": "PerGame", "MeasureType": "Base",
                                 "PORound": "0", "Month": "0", "Period": "0", "LastNGames": "0",
                                 "TeamID": "0", "OpponentTeamID": "0", "Outcome": "",
                                 "Location": "", "SeasonSegment": "", "DateFrom": "",
                                 "DateTo": "", "VsConference": "", "VsDivision": "",
                                 "GameSegment": "", "Conference": "", "Division": "",
                                 "GameScope": "", "PlayerExperience": "", "PlayerPosition": "",
                                 "StarterBench": "", "TwoWay": "0"},
                         headers=NBA_H, timeout=30).json()
        idx = {h: i for i, h in enumerate(j["resultSets"][0]["headers"])}
        pid = next((row[idx["PLAYER_ID"]] for row in j["resultSets"][0]["rowSet"]
                    if W.canon(row[idx["PLAYER_NAME"]]) == W.canon(player)), None)
        if not pid:
            return None
        g = requests.get("https://stats.nba.com/stats/playergamelog",
                         params={"PlayerID": pid, "Season": season,
                                 "SeasonType": "Regular Season", "LeagueID": "10"},
                         headers=NBA_H, timeout=30).json()
        gi = {h: i for i, h in enumerate(g["resultSets"][0]["headers"])}
        want = dt.date.fromisoformat(date)
        for row in g["resultSets"][0]["rowSet"]:
            gd = dt.datetime.strptime(row[gi["GAME_DATE"]], "%b %d, %Y").date()
            if gd == want:
                st = {k: row[gi[k]] for k in ("PTS", "REB", "AST", "FG3M")}
                return _wnba_combo(st, stat)
    except (requests.RequestException, KeyError, ValueError, TypeError):
        pass
    return None


SETTLE = {"mlb": settle_mlb, "wnba": settle_wnba}


# ----------------------------------------------------------------------------- ledger ops
DDL = """CREATE TABLE IF NOT EXISTS bets (
    bet_id TEXT PRIMARY KEY, placed_at TEXT, sport TEXT, event TEXT, player TEXT,
    stat TEXT, line REAL, side TEXT, odds_taken REAL, stake_units REAL, fair_prob REAL,
    ev_pct REAL, pinn_line REAL, src TEXT, start_time TEXT, status TEXT,
    close_fair REAL, clv_pct REAL, realized REAL, result TEXT, pnl_units REAL, graded_at TEXT)"""


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def notify_ev(bets):
    """Push new +EV bets (mlb/wnba) to ntfy. Deduped by bet_id upstream, so each fires once;
    naturally infrequent (only when a genuinely new edge appears). NTFY_TOPIC from env."""
    import os
    topic = os.environ.get("NTFY_TOPIC")
    # notify only DIRECT (model-free, sharp) edges >=5% — model-priced alts are the least
    # reliable and would flood; they stay logged in the ledger for CLV validation instead.
    strong = sorted((b for b in bets if b["ev"] >= 0.05 and b.get("src") == "direct"),
                    key=lambda b: -b["ev"])
    if not topic or not strong:
        return
    lines = [f"{b['sport'].upper()}: {b['player']} {b['stat']} {b['side']} {b['line']} "
             f"@ {b['odds']:.2f}  (+{b['ev']*100:.0f}% EV vs sharp)" for b in strong[:12]]
    try:
        requests.post(f"https://ntfy.sh/{topic}", data="\n".join(lines).encode("utf-8"),
                      headers={"Title": "Sports +EV bets", "Tags": "moneybag"}, timeout=15)
    except requests.RequestException:
        pass


def cycle():
    con = sqlite3.connect(LEDGER)
    con.execute(DDL)
    ts = now()
    # 1) flag + log new bets
    added = 0
    new_bets = []
    for sport in SPORTS:
        for b in flag(sport):
            bid = bet_id(b)
            if con.execute("SELECT 1 FROM bets WHERE bet_id=?", (bid,)).fetchone():
                continue
            con.execute("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (bid, ts, b["sport"], b["event"], b["player"], b["stat"], b["line"],
                         b["side"], b["odds"], 1.0, b["fair"], b["ev"] * 100, b["pinn_line"],
                         b["src"], b["start"], "open", None, None, None, None, None, None))
            added += 1
            new_bets.append(b)
    con.commit()
    notify_ev(new_bets)                            # push new +EV bets to phone (once each)
    # 2) close + CLV (game started, not yet closed)
    closed = 0
    for row in con.execute("SELECT bet_id,sport,player,stat,line,side,odds_taken,start_time "
                           "FROM bets WHERE status='open'").fetchall():
        bid, sport, player, stat, line, side, odds, start = row
        if not start or _pre(start, ts):          # not started yet
            continue
        cf = closing_fair(sport, player, stat, line, start)
        if cf is None:
            continue
        cf_side = cf if side == "over" else 1 - cf
        clv = (cf_side * odds - 1) * 100
        con.execute("UPDATE bets SET status='closed', close_fair=?, clv_pct=? WHERE bet_id=?",
                    (cf_side, clv, bid))
        closed += 1
    con.commit()
    # 3) grade (game final)
    graded = 0
    for row in con.execute("SELECT bet_id,sport,player,stat,line,side,odds_taken,start_time "
                           "FROM bets WHERE status IN ('open','closed') AND result IS NULL"
                           ).fetchall():
        bid, sport, player, stat, line, side, odds, start = row
        try:
            fin = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00")) \
                + dt.timedelta(hours=FINAL_BUFFER_H)
            if dt.datetime.now(dt.timezone.utc) < fin:
                continue
        except ValueError:
            continue
        realized = SETTLE[sport](player, stat, start)
        if realized is None:
            continue
        if realized == line:
            result, pnl = "push", 0.0
        else:
            over = realized > line
            won = over if side == "over" else not over
            result, pnl = ("W", odds - 1) if won else ("L", -1.0)
        con.execute("UPDATE bets SET status='graded', realized=?, result=?, pnl_units=?, "
                    "graded_at=? WHERE bet_id=?", (realized, result, pnl, ts, bid))
        graded += 1
    con.commit()
    con.close()
    return added, closed, graded


# ----------------------------------------------------------------------------- report
def _agg(con, where=""):
    g = con.execute(f"SELECT result, pnl_units, clv_pct FROM bets WHERE result IS NOT NULL{where}"
                    ).fetchall()
    w = sum(1 for r in g if r[0] == "W"); l = sum(1 for r in g if r[0] == "L")
    p = sum(1 for r in g if r[0] == "push")
    pnl = sum(r[1] or 0 for r in g)
    clvs = [c[0] for c in con.execute(
        f"SELECT clv_pct FROM bets WHERE clv_pct IS NOT NULL{where}").fetchall()]
    n = w + l + p
    roi = (pnl / n * 100) if n else 0.0
    avg_clv = (sum(clvs) / len(clvs)) if clvs else None
    return w, l, p, pnl, roi, avg_clv, len(clvs)


def _health():
    """Data-freshness line. FanDuel's token rotates and will eventually die — surface it
    loudly so it gets refreshed (the one thing that isn't self-healing)."""
    fd = _rows(FD_DB, "fd_lines")
    fsnap, flive = _latest(fd)
    n = len(flive)
    if not fsnap or n == 0:
        return "⚠️ **FanDuel returned 0 lines** — the `FD_AK` token likely rotated; refresh " \
               "it from the site (env `FD_AK`) or no new bets get logged."
    try:
        age = (dt.datetime.fromisoformat(now()) - dt.datetime.fromisoformat(str(fsnap))
               ).total_seconds() / 3600
        if age > 3:
            return f"⚠️ FanDuel snapshot is {age:.1f}h old ({n} lines) — collection may be " \
                   "stalled; check the `FD_AK` token."
    except ValueError:
        pass
    return f"Data OK — FanDuel {n} lines @ `{fsnap}`."


def report():
    con = sqlite3.connect(LEDGER)
    con.execute(DDL)
    total = con.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    open_n = con.execute("SELECT COUNT(*) FROM bets WHERE status='open'").fetchone()[0]
    w, l, p, pnl, roi, avg_clv, nclv = _agg(con)
    lines = ["# Bet ledger — automated results, CLV & P&L", "",
             f"_{now()} UTC_ · 1 unit = ${UNIT_USD:.0f} · flag threshold +{MIN_EV*100:.0f}% EV", "",
             f"- **Record:** {w}-{l}" + (f"-{p}" if p else "")
             + f"  ·  **P&L:** {pnl:+.2f}u (${pnl*UNIT_USD:+,.0f})  ·  **ROI:** {roi:+.1f}%",
             f"- **Avg CLV:** " + (f"{avg_clv:+.2f}%" if avg_clv is not None else "n/a")
             + f" over {nclv} closed bets  ·  **Open:** {open_n}  ·  **Total logged:** {total}", ""]
    lines += ["> CLV is the signal that matters — positive average CLV means the edge is real "
              "even before the W-L catches up. W-L over small samples is noise.", "",
              _health(), ""]
    # by sport + stat
    breakdown = con.execute(
        "SELECT sport, stat, COUNT(*), SUM(pnl_units), AVG(clv_pct) FROM bets "
        "GROUP BY sport, stat ORDER BY sport, stat").fetchall()
    if breakdown:
        lines += ["### by sport / stat", "",
                  "| sport | stat | bets | settled P&L (u) | avg CLV |",
                  "|---|---|---|---|---|"]
        for sp, st, cnt, spnl, sclv in breakdown:
            lines.append(f"| {sp} | {st} | {cnt} | "
                         f"{(spnl or 0):+.2f} | {('%+.2f%%' % sclv) if sclv is not None else '—'} |")
        lines.append("")
    # recent graded
    recent = con.execute("SELECT graded_at,sport,player,stat,line,side,odds_taken,result,"
                         "realized,pnl_units,clv_pct FROM bets WHERE result IS NOT NULL "
                         "ORDER BY graded_at DESC LIMIT 25").fetchall()
    if recent:
        lines += ["### recent settled bets", "",
                  "| date | sport | player | bet | odds | result | got | P&L | CLV |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for ga, sp, pl, st, ln, sd, od, res, rz, pn, cl in recent:
            lines.append(f"| {str(ga)[:10]} | {sp} | {pl} | {st} {sd} {ln} | {od:.2f} | "
                         f"{res} | {rz:g} | {pn:+.2f}u | {('%+.1f%%' % cl) if cl is not None else '—'} |")
        lines.append("")
    con.close()
    REPORT.write_text("\n".join(lines) + "\n")
    return "\n".join(lines)


def main():
    filt = HERE / "bet_filters.json"
    if not filt.exists():                      # so the daily learner's file always exists
        filt.write_text('{"benched": []}\n')
    added, closed, graded = cycle()
    print(f"[{now()}] ledger: +{added} new, {closed} closed(CLV), {graded} graded")
    print(report())


if __name__ == "__main__":
    main()
