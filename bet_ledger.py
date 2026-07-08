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
MIN_EV_MODEL = 0.04         # model-priced EV inside model error isn't edge — higher bar
MAX_EV = 0.30               # a real edge vs a sharp de-vig is small; >this = artifact, skip
MAX_EV_MODEL = 0.12         # a MODEL claiming >12% vs sharp anchors is betting its own
                            # error (live proof: EV 10-30% band realized -40% ROI)
MODEL_BAND = (0.20, 0.90)   # only model-price alt lines in this fair-prob band (not deep tails)
FINAL_BUFFER_H = 4.0        # a game is assumed final this many hours after first pitch/tip

SPORTS = {
    "mlb":  {"db": HERE / "mlb_kprops.sqlite", "table": "pitcher_props", "pcol": "pitcher",
             "model": True},
    "wnba": {"db": HERE / "wnba_props.sqlite", "table": "wnba_props", "pcol": "player",
             "model": True},
    "tennis": {"db": HERE / "odds.sqlite", "table": "odds", "custom": True},
    # H2H GG League esports (FanDuel): bets are inserted by gg_collect.py, not by
    # flag(); settlement/CLV come from gg.sqlite (hudstats results + our FD quotes).
    "esoccer":     {"external": True},
    "ebasketball": {"external": True},
    "efootball":   {"external": True},
}
GG_DB = HERE / "gg.sqlite"
MAX_SNAP_DRIFT_H = 2.0      # skip flagging when FD vs sharp snapshots are this far apart
                            # (a stale side manufactures phantom EV)


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
    """Buckets the learner has benched — (sport, stat, src) triples, with legacy
    (sport, stat) pairs applying to every src."""
    import json
    f = HERE / "bet_filters.json"
    if not f.exists():
        return set()
    try:
        return {tuple(x) for x in json.loads(f.read_text()).get("benched", [])}
    except (ValueError, OSError):
        return set()


def _is_benched(benched, sport, stat, src):
    return (sport, stat) in benched or (sport, stat, src) in benched


def _ev_ok(b):
    if b.get("src") == "model":
        return MIN_EV_MODEL <= b["ev"] <= MAX_EV_MODEL
    return MIN_EV <= b["ev"] <= MAX_EV


def _mkbet(sport, r, p_side, pinn_line, start, src):
    return {"sport": sport, "event": r["event"], "player": r["player"], "stat": r["stat"],
            "line": float(r["line"]), "side": r["side"], "odds": float(r["odds"]),
            "fair": p_side, "ev": p_side * float(r["odds"]) - 1, "pinn_line": pinn_line,
            "start": start, "src": src, "book": r.get("book", "fd")}


def _drifted(snap_a, snap_b):
    try:
        a = dt.datetime.fromisoformat(str(snap_a)[:19])
        b = dt.datetime.fromisoformat(str(snap_b)[:19])
        return abs((a - b).total_seconds()) > MAX_SNAP_DRIFT_H * 3600
    except (ValueError, TypeError):
        return True


def _best_book_lines(sport):
    """Latest snapshot PER BOOK, then for each (player, stat, line, side) keep the
    single BEST (highest) price across books — line shopping. Each returned row keeps
    the winning book in 'book'. (fsnap = newest snapshot across books, for drift.)"""
    rows = _rows(FD_DB, "fd_lines", f" WHERE sport='{sport}'")
    if not rows:
        return None, []
    for r in rows:
        r.setdefault("book", "fd")
    latest_per_book = {}
    for r in rows:                                    # newest collected_at within each book
        latest_per_book[r["book"]] = max(latest_per_book.get(r["book"], ""),
                                         str(r["collected_at"]))
    live = [r for r in rows if str(r["collected_at"]) == latest_per_book[r["book"]]]
    best = {}
    for r in live:
        k = (W.canon(r["player"]), r["stat"], round(float(r["line"]), 1), r["side"])
        if k not in best or float(r["odds"]) > float(best[k]["odds"]):
            best[k] = r
    fsnap = max(latest_per_book.values()) if latest_per_book else None
    return fsnap, list(best.values())


def flag(sport):
    """Return [dict] of +EV bets on the latest pre-game snapshot for `sport`."""
    cfg = SPORTS[sport]
    if cfg.get("external"):
        return []                        # logged directly by gg_collect.py
    if cfg.get("custom"):
        return flag_tennis()
    benched = _benched()
    psnap, plive = _latest(_rows(cfg["db"], cfg["table"]))
    if not plive:
        return []
    plive = [r for r in plive if r.get("start_time") and _pre(r["start_time"], psnap)]
    fsnap, fd = _best_book_lines(sport)
    if not (plive and fd) or _drifted(psnap, fsnap):
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
    return [b for b in bets if _ev_ok(b)
            and not _is_benched(benched, sport, b["stat"], b.get("src"))]


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


# ----------------------------------------------------------------------------- tennis
# FanDuel posts PLAYER total-games lines ("Jannik Sinner Total Games 19.5"). Pinnacle
# doesn't post that market, but its Games matchup carries two sharp anchors — the match
# games TOTAL and the games SPREAD. Player games = (total + own margin) / 2, so a Normal
# with means inverted from both Shin-devigged anchors prices the player line. Everything
# here is src="model": logged to the ledger for CLV validation (the daily learner benches
# it if realized CLV goes negative), and NOT phone-pushed.
from statistics import NormalDist
_TN = NormalDist()
TEN_SD = {3: (5.8, 5.5), 5: (8.0, 7.5)}     # (sd_total, sd_margin) by best-of


def _clampp(p):
    return min(max(p, 1e-6), 1 - 1e-6)


def _tennis_means(r):
    """(mu_home_games, mu_away_games, sd_player) from one odds-snapshot row, or None."""
    po = W.fair_prob(r.get("games_over"), r.get("games_under"))
    ph = W.fair_prob(r.get("gspr_home"), r.get("gspr_away"))
    T, h = r.get("games_line"), r.get("games_spread")
    if None in (po, ph, T, h):
        return None
    sd_t, sd_m = TEN_SD[5] if (r.get("best_of") or 3) >= 5 else TEN_SD[3]
    mu_t = T + sd_t * _TN.inv_cdf(_clampp(po))          # P(total > T) = po
    mu_m = -h + sd_m * _TN.inv_cdf(_clampp(ph))         # P(home margin > -h) = ph
    sd_p = ((sd_t ** 2 + sd_m ** 2) ** 0.5) / 2
    return (mu_t + mu_m) / 2, (mu_t - mu_m) / 2, sd_p


def _tennis_prob_over(r, player, line):
    """Model P(player's games > line) for whichever side of the match `player` is."""
    mm = _tennis_means(r)
    if mm is None:
        return None
    mu_h, mu_a, sd = mm
    if W.canon(player) == W.canon(r["p1"]):
        mu = mu_h
    elif W.canon(player) == W.canon(r["p2"]):
        mu = mu_a
    else:
        return None
    return 1 - _TN.cdf((line - mu) / sd)


def flag_tennis_matchtotal():
    """DIRECT +EV: FanDuel MATCH total games (match_games) vs Pinnacle's own games
    line at the EXACT same number — model-free, the safest tennis edge. FD event name
    'A v B' is matched to a Pinnacle row by both surnames."""
    psnap, plive = _latest(_rows(SPORTS["tennis"]["db"], "odds"))
    fsnap, fd = _latest(_rows(FD_DB, "fd_lines", " WHERE sport='tennis'"))
    if not (plive and fd) or _drifted(psnap, fsnap):
        return []
    # index Pinnacle games line by surname pair
    pinn = {}
    for r in plive:
        gl, go, gu = r.get("games_line"), r.get("games_over"), r.get("games_under")
        p = W.fair_prob(go, gu)
        if gl is None or p is None or not r.get("start_time") or not _pre(r["start_time"], psnap):
            continue
        key = frozenset((W.canon(str(r["p1"]).split()[-1]),
                         W.canon(str(r["p2"]).split()[-1])))
        pinn[key] = (round(float(gl), 1), p, r["start_time"], f"{r['p1']} v {r['p2']}")
    bets = []
    for f in fd:
        if f["stat"] != "match_games" or f["line"] is None:
            continue
        ev = str(f["player"])                          # 'A v B'
        if " v " not in ev:
            continue
        a, b = ev.split(" v ", 1)
        key = frozenset((W.canon(a.split()[-1]), W.canon(b.split()[-1])))
        hit = pinn.get(key)
        if not hit or hit[0] != round(float(f["line"]), 1):
            continue                                   # need the EXACT same line
        _, p_over, start, label = hit
        p_side = p_over if f["side"] == "over" else 1 - p_over
        ev_pct = p_side * float(f["odds"]) - 1
        if MIN_EV <= ev_pct <= MAX_EV:
            bets.append({"sport": "tennis", "event": label, "player": ev,
                         "stat": "match_games", "line": float(f["line"]),
                         "side": f["side"], "odds": float(f["odds"]), "fair": p_side,
                         "ev": ev_pct, "pinn_line": hit[0], "start": start,
                         "src": "direct", "book": "fd"})
    return bets


def flag_tennis():
    """+EV FanDuel player-games bets vs the Pinnacle-anchored model (singles only).
    Plus the model-free MATCH-total-games direct edge."""
    return _flag_tennis_playergames() + flag_tennis_matchtotal()


def _flag_tennis_playergames():
    benched = _benched()
    psnap, plive = _latest(_rows(SPORTS["tennis"]["db"], "odds"))
    if not plive:
        return []
    plive = [r for r in plive if r.get("start_time") and _pre(r["start_time"], psnap)]
    fsnap, fd = _latest(_rows(FD_DB, "fd_lines", " WHERE sport='tennis'"))
    if not (plive and fd) or _drifted(psnap, fsnap):
        return []
    byplayer = {}
    for r in plive:
        byplayer[W.canon(r["p1"])] = r
        byplayer[W.canon(r["p2"])] = r
    bets = []
    for f in fd:
        if f["stat"] != "player_games" or f["line"] is None or "/" in (f["player"] or ""):
            continue
        r = byplayer.get(W.canon(f["player"]))
        if r is None:
            continue
        p = _tennis_prob_over(r, f["player"], float(f["line"]))
        if p is None or not (MODEL_BAND[0] <= p <= MODEL_BAND[1]):
            continue
        p_side = p if f["side"] == "over" else 1 - p
        ev = p_side * float(f["odds"]) - 1
        if MIN_EV_MODEL <= ev <= MAX_EV_MODEL \
                and not _is_benched(benched, "tennis", "player_games", "model"):
            bets.append({"sport": "tennis", "event": f"{r['p1']} v {r['p2']}",
                         "player": f["player"], "stat": "player_games",
                         "line": float(f["line"]), "side": f["side"],
                         "odds": float(f["odds"]), "fair": p_side, "ev": ev,
                         "pinn_line": r.get("games_line"), "start": r["start_time"],
                         "src": "model"})
    return bets


def closing_fair_tennis(player, line, start):
    """Model prob at Pinnacle's last pre-start snapshot (same model both ends, so CLV
    measures pure line movement). Matches the MATCH by player + start within 6h —
    Pinnacle shifts start_time between snapshots on delays, so exact-equality would
    silently drop the close and leave the bucket CLV-blind."""
    def _ts(s):
        try:
            return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return None
    t0 = _ts(start)
    if t0 is None:
        return None
    rows = []
    for r in _rows(SPORTS["tennis"]["db"], "odds"):
        if str(r["collected_at"])[:19] > str(start)[:19]:
            continue
        if W.canon(r["p1"]) != W.canon(player) and W.canon(r["p2"]) != W.canon(player):
            continue
        t = _ts(r.get("start_time"))
        if t is None or abs((t - t0).total_seconds()) > 6 * 3600:
            continue
        rows.append(r)
    if not rows:
        return None
    close_ts = max(r["collected_at"] for r in rows)
    r = next(r for r in rows if r["collected_at"] == close_ts)
    return _tennis_prob_over(r, player, float(line))


# 24live day list (free, datacenter-OK with browser-ish headers) settles tennis bets.
LIVE24_H = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 "
                           "Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://24live.com/", "X-Requested-With": "XMLHttpRequest"}
_ten_day_cache = {}


def _live24_finished_tennis(day):
    """All finished tennis matches for a UTC day: [{home, away, hg, ag, start}]."""
    if day in _ten_day_cache:
        return _ten_day_cache[day]
    import urllib.parse as up
    fr, to = f"{day} 00:00:00", f"{day} 23:59:59"
    out = []
    try:
        P = {"lang": "en", "type": "finished", "sort": "tournament",
             "from": fr, "to": to, "category": ""}
        q = "&".join(f"{k}={up.quote(v)}" for k, v in P.items())
        cats = requests.get(f"https://24live.com/api/match-list-category/10?{q}",
                            headers=LIVE24_H, timeout=40).json()
        subs = ",".join(str(c["sub_tournament_id"]) for c in cats)
        P2 = {"lang": "en", "type": "finished", "subtournamentIds": subs,
              "sort": "tournament", "short": "0", "from": fr, "to": to,
              "defaultSubtournamentLimit": "500"}
        q2 = "&".join(f"{k}={up.quote(v)}" for k, v in P2.items())
        for m in requests.get(f"https://24live.com/api/match-list-data/10?{q2}",
                              headers=LIVE24_H, timeout=60).json():
            parts = [p.get("name") or "" for p in m.get("participants") or []]
            sc = m.get("score") or {}
            if len(parts) != 2 or any("/" in p for p in parts):
                continue
            hg, ag = sc.get("home_team_normal_time"), sc.get("away_team_normal_time")
            if hg is None or ag is None:
                continue
            out.append({"home": parts[0], "away": parts[1], "hg": hg, "ag": ag,
                        "start": m.get("start_date")})
    except (requests.RequestException, ValueError, KeyError):
        pass
    _ten_day_cache[day] = out
    return out


def _surname24(name):
    """24live is 'Surname(s) Given' -> canon of all-but-last token."""
    toks = name.split()
    return W.canon(" ".join(toks[:-1]) if len(toks) > 1 else name)


def _sur_match(a, b):
    return a and b and (a in b or b in a)


def _settle_match_games(event, start):
    """Total games in the match = home_games + away_games from 24live's finished list,
    matched by BOTH surnames in the 'A v B' event string + start proximity."""
    if " v " not in str(event):
        return None
    a, b = str(event).split(" v ", 1)
    sa, sb = W.canon(a.split()[-1]), W.canon(b.split()[-1])
    day = str(start)[:10]
    best = None
    for d in (day, (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()):
        for m in _live24_finished_tennis(d):
            hs, aw = _surname24(m["home"]), _surname24(m["away"])
            if not ((_sur_match(sa, hs) and _sur_match(sb, aw)) or
                    (_sur_match(sa, aw) and _sur_match(sb, hs))):
                continue
            try:
                gap = abs((dt.datetime.fromisoformat(str(m["start"]).replace("Z", "+00:00"))
                           - dt.datetime.fromisoformat(str(start).replace("Z", "+00:00").replace(" ", "T"))
                           ).total_seconds())
            except ValueError:
                gap = 9e9
            if gap <= 8 * 3600 and (best is None or gap < best[0]):
                best = (gap, float(m["hg"]) + float(m["ag"]))
    return best[1] if best else None


def settle_tennis(player, stat, start, event=None):
    """Player's games won (player_games), or BOTH players' games summed (match_games),
    from 24live's finished tennis list. Identity binds on BOTH players' surnames — one
    surname alone can hit the wrong match when two Zhangs play the same day. Surname
    containment absorbs 'Gauff Cori' vs 'Coco Gauff' drift. Start proximity (8h) is a
    tiebreak, not the identity."""
    if stat == "match_games":
        return _settle_match_games(player, start)
    surname = W.canon(str(player).split()[-1])
    opp = None
    if event and " v " in str(event):
        a, b = str(event).split(" v ", 1)
        other = b if W.canon(a) == W.canon(player) else a
        opp = W.canon(other.split()[-1])
    day = str(start)[:10]
    cands = []
    for d in (day, (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()):
        for m in _live24_finished_tennis(d):
            for side, gkey in (("home", "hg"), ("away", "ag")):
                oside = "away" if side == "home" else "home"
                if not _sur_match(surname, _surname24(m[side])):
                    continue
                if opp and not _sur_match(opp, _surname24(m[oside])):
                    continue
                try:
                    gap = abs((dt.datetime.fromisoformat(str(m["start"]).replace("Z", "+00:00"))
                               - dt.datetime.fromisoformat(str(start).replace("Z", "+00:00").replace(" ", "T"))
                               ).total_seconds())
                except ValueError:
                    gap = 9e9
                cands.append((gap, float(m[gkey])))
    if not cands:
        return None
    gap, games = min(cands, key=lambda c: c[0])
    return games if gap <= 8 * 3600 else None


def bet_id(b):
    day = str(b["start"])[:10]
    return f"{b['sport']}|{W.canon(b['player'])}|{b['stat']}|{b['line']}|{b['side']}|{day}"


# ----------------------------------------------------------------------------- closing / CLV
def closing_fair_gg(sport, player, line, start):
    """FanDuel's own de-vigged closing prob for the exact line, from our gg_quotes
    snapshots (last one at/before start). None until enough snapshots exist."""
    if not GG_DB.exists():
        return None
    nicks = [n.strip() for n in str(player).split(" v ")]
    if len(nicks) != 2:
        return None
    con = sqlite3.connect(GG_DB)
    try:
        rows = con.execute(
            "SELECT collected_at, over_odds, under_odds FROM gg_quotes WHERE line=? "
            "AND ((p1=? AND p2=?) OR (p1=? AND p2=?)) AND start=? "
            "AND collected_at<=? ORDER BY collected_at DESC LIMIT 1",
            (line, nicks[0], nicks[1], nicks[1], nicks[0], str(start),
             str(start)[:19])).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    return W.fair_prob(rows[0][1], rows[0][2]) if rows else None


def settle_gg(player, stat, start, event=None):
    """Realized total from hudstats results: match by nickname pair + start ±30 min."""
    if not GG_DB.exists():
        return None
    nicks = [n.strip() for n in str(player).split(" v ")]
    if len(nicks) != 2:
        return None
    con = sqlite3.connect(GG_DB)
    try:
        rows = con.execute(
            "SELECT start, total FROM gg_matches WHERE (p1=? AND p2=?) OR (p1=? AND p2=?)",
            (nicks[0], nicks[1], nicks[1], nicks[0])).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    try:
        t0 = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None
    best = None
    for s, tot in rows:
        try:
            gap = abs((dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")) - t0
                       ).total_seconds())
        except ValueError:
            continue
        if gap <= 1800 and (best is None or gap < best[0]):
            best = (gap, tot)
    return float(best[1]) if best else None


def _closing_match_games(event, line, start):
    """Pinnacle's devigged fair P(over) on the match games line, at the last snapshot
    before start — direct close for a match_games bet (measures real CLV)."""
    if " v " not in str(event):
        return None
    a, b = str(event).split(" v ", 1)
    sa, sb = W.canon(a.split()[-1]), W.canon(b.split()[-1])
    rows = [r for r in _rows(SPORTS["tennis"]["db"], "odds")
            if r.get("games_line") is not None
            and str(r["collected_at"])[:19] <= str(start)[:19]
            and frozenset((W.canon(str(r["p1"]).split()[-1]),
                           W.canon(str(r["p2"]).split()[-1]))) == frozenset((sa, sb))]
    if not rows:
        return None
    r = max(rows, key=lambda x: x["collected_at"])
    if round(float(r["games_line"]), 1) != round(float(line), 1):
        return None
    return W.fair_prob(r.get("games_over"), r.get("games_under"))


def closing_fair(sport, player, stat, line, start):
    """Pinnacle's de-vigged fair prob for this exact line at the last pre-start snapshot;
    for model stats, anchor the closing MAIN line to the alt line. None if unavailable."""
    if sport == "tennis":
        if stat == "match_games":
            return _closing_match_games(player, line, start)
        return closing_fair_tennis(player, line, start)
    if SPORTS.get(sport, {}).get("external"):
        return closing_fair_gg(sport, player, line, start)
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
    if sport == "mlb" and stat == "total_bases":       # batter model at the close — without
        return _closing_mlb_tb(player, line, snap)     # this the bucket is CLV-blind and the
    return None                                        # learner can never bench it


def _closing_mlb_tb(player, line, snap):
    """Closing fair for an alt total-bases line: anchor the batter model to Pinnacle's
    closing MAIN line (mirror of the _model_alts pricing, evaluated at the close)."""
    try:
        from mlb import batter, data
        r = snap[0]
        p0 = W.fair_prob(r["over_odds"], r["under_odds"])
        if p0 is None:
            return None
        season = int(str(r.get("start_time"))[:4])
        lines = {W.canon(v["name"]): v for v in
                 data.all_batter_lines(season, min_pa=30).values() if v.get("name")}
        bl = lines.get(W.canon(player))
        if not bl:
            return None
        proj = batter.anchor(bl, round(float(r["line"]), 1), p0,
                             exp_pa=batter.exp_pa_from(bl))
        return batter.prob_over(proj, round(float(line), 1))
    except Exception:
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

MLB_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         "Accept": "application/json"}


def _mlb_game(event, start):
    """The statsapi game feed (linescore innings) for an MLB event on its date, matched
    by the away/home team names in the FD event string 'Away (P) @ Home (P)'."""
    date = _game_date(start)
    try:
        sched = requests.get("https://statsapi.mlb.com/api/v1/schedule",
                             params={"sportId": 1, "date": date, "hydrate": "linescore"},
                             headers=MLB_H, timeout=25).json()
    except (requests.RequestException, ValueError):
        return None
    teams = [t.split("(")[0].strip().lower()
             for t in str(event).replace(" @ ", "@").split("@")]
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            gt = g.get("teams", {})
            names = [gt.get("away", {}).get("team", {}).get("name", "").lower(),
                     gt.get("home", {}).get("team", {}).get("name", "").lower()]
            if all(any(t in nm or nm.endswith(t.split()[-1]) for nm in names) for t in teams) \
                    and g.get("status", {}).get("abstractGameState") == "Final":
                return g.get("linescore", {})
    return None


def settle_mlb_total(player, stat, start, event):
    ls = _mlb_game(event, start)
    if not ls:
        return None
    innings = ls.get("innings", [])
    if stat == "game_total":
        t = ls.get("teams", {})
        a, h = t.get("away", {}).get("runs"), t.get("home", {}).get("runs")
        return float(a + h) if a is not None and h is not None else None
    if stat == "f5_total":
        tot = 0
        for i in innings[:5]:
            a = (i.get("away") or {}).get("runs")
            h = (i.get("home") or {}).get("runs")
            if a is None or h is None:
                return None                       # game not 5 innings deep yet
            tot += a + h
        return float(tot) if len(innings) >= 5 else None
    if stat.startswith("team_total_"):
        side = stat.split("_")[-1]
        r = ls.get("teams", {}).get(side, {}).get("runs")
        return float(r) if r is not None else None
    return None


def settle_mlb(player, stat, start, event=None):
    if stat in ("game_total", "f5_total") or stat.startswith("team_total_"):
        return settle_mlb_total(player, stat, start, event)
    spec = MLB_FIELD.get(stat)
    if not spec:
        return None
    grp, fld = spec
    date = _game_date(start)
    try:
        s = requests.get("https://statsapi.mlb.com/api/v1/people/search",
                         params={"names": player}, headers=MLB_H,
                         timeout=20).json().get("people") or []
        if not s:
            return None
        g = requests.get(f"https://statsapi.mlb.com/api/v1/people/{s[0]['id']}/stats",
                         params={"stats": "gameLog", "group": grp, "season": date[:4]},
                         headers=MLB_H, timeout=20).json()
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


def settle_wnba(player, stat, start, event=None):
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


def settle_nfl(player, stat, start, event=None):
    """NFL game/team totals from ESPN's scoreboard final scores."""
    date = _game_date(start).replace("-", "")
    try:
        j = requests.get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                         "scoreboard", params={"dates": date}, headers=MLB_H, timeout=25).json()
    except (requests.RequestException, ValueError):
        return None
    teams = [t.split("(")[0].strip().lower() for t in str(event).split(" @ ")]
    for gm in j.get("events", []):
        comp = (gm.get("competitions") or [{}])[0]
        if comp.get("status", {}).get("type", {}).get("state") != "post":
            continue
        cs = comp.get("competitors", [])
        nm = {c.get("homeAway"): (c.get("team", {}).get("displayName", "").lower(),
                                  c.get("score")) for c in cs}
        allnames = [v[0] for v in nm.values()]
        if not all(any(t in n or n.endswith(t.split()[-1]) for n in allnames) for t in teams):
            continue
        try:
            home = float(nm["home"][1]); away = float(nm["away"][1])
        except (KeyError, TypeError, ValueError):
            return None
        if stat == "game_total":
            return home + away
        if stat == "team_total_home":
            return home
        if stat == "team_total_away":
            return away
    return None


SETTLE = {"mlb": settle_mlb, "wnba": settle_wnba, "tennis": settle_tennis,
          "esoccer": settle_gg, "ebasketball": settle_gg, "efootball": settle_gg,
          "nfl": settle_nfl}


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
    lines = [f"{b['sport'].upper()} [{b.get('book', 'fd').upper()}]: {b['player']} "
             f"{b['stat']} {b['side']} {b['line']} @ {b['odds']:.2f}  "
             f"(+{b['ev']*100:.0f}% EV vs sharp)" for b in strong[:12]]
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
    for row in con.execute("SELECT bet_id,sport,player,stat,line,side,odds_taken,start_time,"
                           "event FROM bets WHERE status IN ('open','closed') AND result IS NULL"
                           ).fetchall():
        bid, sport, player, stat, line, side, odds, start, event = row
        try:
            fin = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00")) \
                + dt.timedelta(hours=FINAL_BUFFER_H)
            if dt.datetime.now(dt.timezone.utc) < fin:
                continue
        except ValueError:
            continue
        realized = SETTLE[sport](player, stat, start, event)
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
