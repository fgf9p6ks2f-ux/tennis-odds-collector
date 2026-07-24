"""Forward paper-tracker for the two candidate MLB pitcher-prop edges (2026-07-21).

These are UNCONFIRMED leads found in a 2-week backtest that beat Pinnacle + held across
split-halves — but 2 weeks is too thin to bet real money. This banks the OUT-OF-SAMPLE
evidence forward so we can confirm or kill them honestly (memory: real-lines-only,
validate-before-shipping, no-MAE — record/hit%/ROI only).

  RULE 1  k_over    : bet OVER when a pitcher's STRIKEOUT line is >= 6.5
                      (books anchor high-K arms conservatively; overs underpriced)
  RULE 2  outs_under: bet UNDER when a pitcher's PITCHING-OUTS line is <= 16.5
                      (league-wide starter-length decline; books price outs a touch long)

Each qualifying pitcher-game logs a paper bet at BOTH books' closing line/odds where
available: Pinnacle (mlb_kprops.sqlite — the hard benchmark) and FanDuel (fanduel_props
.sqlite — the real target, softer). Graded vs actual K / outs from statsapi gamelogs.

    python k_paper.py            # flag new + update-to-close + grade finished + report
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mlb import data  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = HERE / "k_paper.sqlite"
PINN = HERE / "mlb_kprops.sqlite"
FD = HERE / "fanduel_props.sqlite"
TOTALS = HERE / "mlb_totals.sqlite"     # Vegas game O/U totals — SHADOW feature source (forward)
IDCACHE = HERE / "k_paper_ids.json"

K_OVER_MIN = 6.5        # RULE 1: strikeout line >= this -> OVER
OUTS_UNDER_MAX = 16.5   # RULE 2: outs line <= this -> UNDER
EPOCH = "2026-07-22"    # games on/after this = the true FORWARD (out-of-sample) test;
#                         earlier games are the in-sample seed (the backtest), shown separately

DDL = """CREATE TABLE IF NOT EXISTS paper (
  pitcher TEXT, game_date TEXT, market TEXT, rule TEXT, book TEXT,
  side TEXT, line REAL, odds REAL, flagged_at TEXT, closed INTEGER DEFAULT 0,
  result TEXT, actual INTEGER, pnl REAL, graded_at TEXT, home INTEGER, opp_k REAL, premium INTEGER,
  PRIMARY KEY (pitcher, game_date, market, book))"""


CONTACT_MAX = 0.225     # opponent team K% below this = a CONTACT offense (balls in play ->
#                         traffic -> higher pitch count -> earlier hook -> outs-under stacks)


def _ensure(con):
    """Create the table + add later columns. The outs-under edge is an AWAY-starter effect that
    STACKS with contact offenses (tag home/away + opp_k), and the ★★ PREMIUM tier stacks further:
    away+contact + (low-patience opp OR line>recent-outs) hit ~+49% ROI (tag `premium`)."""
    con.execute(DDL)
    cols = {r[1] for r in con.execute("PRAGMA table_info(paper)")}
    # SHADOW features (2026-07-23): captured at grade time on NEW bets only (old rows stay NULL =
    # forward-only). NONE feed _qualifies/flag() or _mlb_graded — pure observation, so we can later
    # test whether run-environment / opponent OBP / pitcher role sharpen the outs-under edge without
    # touching the live 30-9 model. game_total = wired for real; the other three are the shadow test.
    # pitches_per_out / pps_ratio added 2026-07-23 — the two leading new-route-B candidates from the
    # premium-stack audit (both starts-only + leak-free). Shadow ONLY until the forward sample supports
    # promoting one to a live gate; the whole A-only+ppo lift was just 6 bets of difference.
    for c, typ in (("home", "INTEGER"), ("opp_k", "REAL"), ("premium", "INTEGER"),
                   ("game_total", "REAL"), ("opp_obp", "REAL"),
                   ("pitcher_gs", "INTEGER"), ("pitcher_avg_outs", "REAL"),
                   ("pitches_per_out", "REAL"), ("pps_ratio", "REAL"),
                   # which statsapi gamelog game this row was graded against — the durable
                   # CROSS-RUN claim so a later run can never re-grade a game already used.
                   ("log_date", "TEXT")):
        if c not in cols:
            con.execute(f"ALTER TABLE paper ADD COLUMN {c} {typ}")


def _team_hit(season):
    """(k_map{tid:K%}, lg_k, ppa_map{tid:pitches/PA}, ppa_median) — for contact + patience tags."""
    tk, lg = data.team_kpct(season)
    ppa = {}
    try:
        raw = data._get("/teams/stats", stats="season", group="hitting", season=season, sportId=1)
        for t in raw["stats"][0]["splits"]:
            st, pa = t["stat"], (t["stat"].get("plateAppearances") or 0)
            pit = st.get("numberOfPitches") or 0
            if pa and pit:
                ppa[t["team"]["id"]] = pit / pa
    except Exception:
        pass
    vals = sorted(ppa.values())
    p25 = vals[int(len(vals) * 0.25)] if vals else 3.82   # genuinely-low-patience threshold (not median)
    return tk, lg, ppa, p25


def _game_total(pitcher, gd):
    """Vegas game O/U total for the pitcher's game on `gd` (SHADOW field — never used by the model
    or the record). Matches mlb_totals rows by the pitcher's LAST NAME inside the event string
    (e.g. 'Diamondbacks (B Pfaadt) @ Cardinals (M McGreevy)'), latest snapshot, date within ±1 day.
    Returns None when the total wasn't collected (fine — forward capture is best-effort)."""
    last = (pitcher or "").split()[-1].lower()
    if not (last and TOTALS.exists()):
        return None
    try:
        con = sqlite3.connect(f"file:{TOTALS}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT line, event FROM totals WHERE market='game_total' AND line>0 "
            "AND date(start_time) BETWEEN date(?, '-1 day') AND date(?, '+1 day') "
            "ORDER BY collected_at DESC", (gd, gd)).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    for line, event in rows:
        if last in (event or "").lower():
            return line
    return None


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def _load_ids():
    if IDCACHE.exists():
        try:
            return json.loads(IDCACHE.read_text())
        except ValueError:
            pass
    return {}


def _closing_lines(db, market_stat):
    """{(pitcher, game_date): (line, over_dec, under_dec, start_iso)} = latest snapshot
    at or before first pitch, for a given prop stat, from a *_props.sqlite line store."""
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    # both stores share (pitcher/player, stat, line, over_odds/under_odds, start_time-ish)
    if db == PINN:
        q = ("SELECT pitcher, date(start_time), line, over_odds, under_odds, start_time, collected_at "
             "FROM pitcher_props WHERE stat=? AND start_time IS NOT NULL AND collected_at<=start_time "
             "ORDER BY pitcher, date(start_time), collected_at")
        rows = con.execute(q, (market_stat,)).fetchall()
    else:  # FanDuel fd_lines: no start_time; use the event day = collected day (props post game-day)
        q = ("SELECT player, date(collected_at), line, side, odds, collected_at "
             "FROM fd_lines WHERE sport='mlb' AND stat=? ORDER BY player, date(collected_at), collected_at")
        raw = con.execute(q, (market_stat,)).fetchall()
        con.close()
        # FD ladder is one-sided rows; fold to (line -> {over,under}) per pitcher-day, take the MAIN
        # line (closest to a fair ~even market) — approximate: the line whose over odds are nearest 1.9
        byg = {}
        for pl, gd, line, side, odds, cat in raw:
            byg.setdefault((pl, gd), {}).setdefault(line, {})[side] = (odds, cat)
        out = {}
        for key, lines in byg.items():
            two = {l: v for l, v in lines.items() if "over" in v and "under" in v}
            pick = None
            if two:
                pick = min(two, key=lambda l: abs(two[l]["over"][0] - 1.9))
                oo, uo = two[pick]["over"][0], two[pick]["under"][0]
            else:  # only over ladder — take the rung nearest even money as the "main"
                pick = min(lines, key=lambda l: abs(lines[l].get("over", (99,))[0] - 1.9))
                oo = lines[pick].get("over", (None,))[0]
                uo = lines[pick].get("under", (None,))[0]
            out[key] = (pick, oo, uo, None)
        return out
    con.close()
    last = {}
    for pitcher, gd, line, oo, uo, start, cat in rows:
        if oo and uo:
            last[(pitcher, gd)] = (line, oo, uo, start)
    return last


def _qualifies(market, line):
    if market == "k" and line is not None and line >= K_OVER_MIN:
        return "over", "k_over"
    if market == "outs" and line is not None and line <= OUTS_UNDER_MAX:
        return "under", "outs_under"
    return None, None


def flag():
    con = sqlite3.connect(DB)
    _ensure(con)
    ts = _now()
    added = updated = 0
    for market, stat, books in (("k", "strikeouts", ((PINN, "pinn"), (FD, "fd"))),
                                ("outs", "outs", ((PINN, "pinn"), (FD, "fd")))):
        for db, book in books:
            for (pitcher, gd), (line, oo, uo, start) in _closing_lines(db, stat).items():
                side, rule = _qualifies(market, line)
                if not side:
                    continue
                odds = oo if side == "over" else uo
                if odds is None:
                    continue
                row = con.execute("SELECT closed FROM paper WHERE pitcher=? AND game_date=? "
                                  "AND market=? AND book=?", (pitcher, gd, market, book)).fetchone()
                if row is None:                        # FIRST time we see it -> FREEZE the flag-time
                    con.execute("INSERT INTO paper (pitcher,game_date,market,rule,book,side,line,"
                                "odds,flagged_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                (pitcher, gd, market, rule, book, side, line, odds, ts))
                    added += 1
                # NOTE (user 2026-07-21): keep the FLAG-TIME line/odds — do NOT refresh toward the
                # close. The price when first flagged is the number we'd have bet (usually the best,
                # before the market moves), so the record is graded/paid at that price. (Applies to
                # every tracker; WNBA/TT already stamp odds at flag time.)
    con.commit()
    con.close()
    print(f"flag: +{added} new")


def grade():
    con = sqlite3.connect(DB)
    _ensure(con)
    ids = _load_ids()
    today = dt.date.today().isoformat()
    todo = con.execute("SELECT DISTINCT pitcher, game_date FROM paper WHERE result IS NULL "
                       "AND game_date < ?", (today,)).fetchall()
    from collections import defaultdict
    by_pitcher = defaultdict(list)
    for pitcher, gd in todo:
        by_pitcher[pitcher].append(gd)

    def _d(s):
        try:
            return dt.date.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    logcache = {}
    tkcache = {}
    obpcache = {}                                             # season -> {team_id: OBP} (shadow)
    graded = 0
    for pitcher, gds in by_pitcher.items():
        pid = ids.get(pitcher)
        if pid is None and pitcher not in ids:
            pid = data.find_pitcher(pitcher)
            ids[pitcher] = pid
        if not pid:
            continue
        if pid not in logcache:
            try:
                logcache[pid] = data.pitcher_gamelog(pid, int(gds[0][:4]))
            except Exception:
                logcache[pid] = []
        # final games only (bf>=5), keyed by the statsapi ET gamelog date
        logby = {}
        for x in logcache[pid]:
            if (x.get("bf") or 0) >= 5 and x.get("date"):
                logby[x["date"]] = x
        # ── CROSS-RUN CLAIM (2026-07-24 fix, user caught Anthony Kay double-counted): claim-once
        # below only dedupes WITHIN one grade() call — `todo` holds ungraded rows, so a game already
        # graded in an EARLIER run is invisible and the ±1-day fallback happily re-claims it. That's
        # how Kay/Rea/Montero's 7/22 starts got graded a second time under a phantom 7/23 game_date
        # (2 fake W + 1 fake L). Seed `claimed` from rows this pitcher ALREADY has graded: prefer the
        # stored log_date, and for legacy rows (pre-column) infer it by matching outs within ±1 day.
        pre_claimed = set()
        for _gd0, _act0, _ld0 in con.execute(
                "SELECT game_date, actual, log_date FROM paper WHERE pitcher=? "
                "AND result IN ('W','L')", (pitcher,)).fetchall():
            if _ld0:
                pre_claimed.add(_ld0)
                continue
            for _d0, _x0 in logby.items():                      # legacy: infer which game it used
                if _x0.get("outs") == _act0 and _d(_d0) and _d(_gd0) \
                        and abs((_d(_d0) - _d(_gd0)).days) <= 1:
                    pre_claimed.add(_d0)
                    break
        # ── CLAIM-ONCE matching (2026-07-23 fix): a real game grades EXACTLY ONCE. Exact date
        # first, then ±1-day (the night-game UTC-vs-ET shift), never reusing a game already claimed
        # by another game_date. A game_date left unmatched but with a CLAIMED ±1 neighbour is a
        # phantom DUPLICATE (the 7/21-ET game re-logged under 7/22-UTC) -> voided, not double-counted.
        match, claimed = {}, set(pre_claimed)                     # seeded with prior runs' claims
        for gd in sorted(gds):                                   # 1) exact
            if gd in logby and gd not in claimed:
                claimed.add(gd); match[gd] = logby[gd]
        for gd in sorted(gds):                                   # 2) ±1 fallback, closest unclaimed
            if gd in match:
                continue
            gdd = _d(gd)
            if not gdd:
                continue
            cands = sorted([d for d in logby if d not in claimed and _d(d)
                            and abs((_d(d) - gdd).days) <= 1],
                           key=lambda d: abs((_d(d) - gdd).days))
            if cands:
                claimed.add(cands[0]); match[gd] = logby[cands[0]]

        season = int(gds[0][:4])
        if season not in tkcache:
            try:
                tkcache[season] = _team_hit(season)
            except Exception:
                tkcache[season] = ({}, 0.22, {}, 3.9)
        tk, lg_k, ppa_map, ppa_low = tkcache[season]

        for gd in gds:
            g = match.get(gd)
            if g is None:
                gdd = _d(gd)
                had_near = any(_d(d) and gdd and abs((_d(d) - gdd).days) <= 1 for d in logby)
                if had_near:                                     # duplicate of an already-graded game
                    con.execute("UPDATE paper SET result='void', actual=NULL, pnl=0, graded_at=?, "
                                "closed=1 WHERE pitcher=? AND game_date=? AND result IS NULL",
                                (_now(), pitcher, gd))
                continue                                         # else: not final / scratched -> leave
            opp_k = tk.get(g.get("opp_id"), lg_k)
            opp_ppa = ppa_map.get(g.get("opp_id"))
            priors = sorted([x for x in logcache[pid] if x.get("date") and x["date"] < g["date"]
                             and (x.get("bf") or 0) >= 5], key=lambda x: x["date"])
            _l5 = sorted(x["outs"] for x in priors[-5:])
            r5 = (_l5[len(_l5) // 2] if len(_l5) % 2 else (_l5[len(_l5) // 2 - 1] + _l5[len(_l5) // 2]) / 2) \
                if len(priors) >= 3 else None
            # ── SHADOW features (forward-only; do NOT gate the bet or the record) ──
            if season not in obpcache:
                try:
                    obpcache[season] = data.team_obp(season)[0]
                except Exception:
                    obpcache[season] = {}
            sh_gtot = _game_total(pitcher, gd)                       # run environment
            sh_oobp = obpcache[season].get(g.get("opp_id"))          # opponent on-base
            sh_pgs = len(priors) + 1                                 # pitcher pedigree = start # this yr
            sh_pavg = round(sum(x["outs"] for x in priors) / len(priors), 2) if priors else None  # workload
            # `priors` is ALREADY starts-only — mlb.data.pitcher_gamelog skips any split with
            # gamesStarted=0, so relief never enters. Do NOT add an outs/pitches "is it a start?"
            # heuristic: it can't distinguish a short START from relief and deletes exactly the
            # blow-up starts (May 6-out/44p and 2-out/34p, both gamesStarted=1) that matter most.
            _st = priors
            # ★ the leading new-route-B candidate: recent PITCHES PER OUT. This is literally the rate
            # he burns his pitch limit (~95 pitches / 5.7 per out ~= 16.7 outs), so HIGH = inefficient
            # = pulled earlier = outs-under wins. Mechanism-sound AND leak-free (prior starts only),
            # unlike the dropped patience route. Audit: high half 20-3 vs low 14-8; 5-0 at FD.
            _po = [(x.get("pitches") or 0, x.get("outs") or 0) for x in _st[-5:] if (x.get("pitches") or 0) > 0]
            _to = sum(o for _, o in _po)
            sh_ppo = round(sum(p for p, _ in _po) / _to, 3) if _to else None
            # recent pitches/START vs his own season baseline (user's idea): <1 = going shorter than
            # his norm = leash tightening -> under. Audit: low half 16-4 (80%), 4-0 at FD.
            _r3 = [x.get("pitches") or 0 for x in _st[-3:] if (x.get("pitches") or 0) > 0]
            _al = [x.get("pitches") or 0 for x in _st if (x.get("pitches") or 0) > 0]
            sh_pps = round((sum(_r3) / len(_r3)) / (sum(_al) / len(_al)), 3) if (_r3 and len(_al) >= 4) else None
            for market, keyk in (("k", "k"), ("outs", "outs")):
                for (side, line, odds) in con.execute(
                        "SELECT side, line, odds FROM paper WHERE pitcher=? AND game_date=? AND market=? "
                        "AND result IS NULL", (pitcher, gd, market)).fetchall():
                    actual = g[keyk]
                    if actual == line:
                        res, pnl = "push", 0.0
                    else:
                        won = (actual > line) if side == "over" else (actual < line)
                        res, pnl = ("W", odds - 1) if won else ("L", -1.0)
                    home = 1 if g.get("is_home") else 0
                    premium = 1 if (market == "outs" and (
                        (opp_ppa is not None and opp_ppa < ppa_low) or (r5 is not None and line > r5))) else 0
                    con.execute("UPDATE paper SET result=?, actual=?, pnl=?, graded_at=?, closed=1, "
                                "home=?, opp_k=?, premium=?, game_total=?, opp_obp=?, pitcher_gs=?, "
                                "pitcher_avg_outs=?, pitches_per_out=?, pps_ratio=?, log_date=? "
                                "WHERE pitcher=? AND game_date=? AND market=? "
                                "AND result IS NULL",
                                (res, actual, pnl, _now(), home, opp_k, premium,
                                 sh_gtot, sh_oobp, sh_pgs, sh_pavg, sh_ppo, sh_pps,
                                 g.get("date"), pitcher, gd, market))
                    graded += 1
    con.commit()
    con.close()
    IDCACHE.write_text(json.dumps(ids))
    print(f"grade: settled {graded}")


def _bucket(con, rule, book, where, args):
    g = con.execute(f"SELECT COUNT(*), SUM(result='W'), SUM(result='L'), COALESCE(SUM(pnl),0) "
                    f"FROM paper WHERE rule=? AND book=? AND result IN ('W','L'){where}",
                    (rule, book, *args)).fetchone()
    n, w, l, pnl = g[0], g[1] or 0, g[2] or 0, g[3] or 0
    return n, w, l, pnl


def report():
    con = sqlite3.connect(DB)
    _ensure(con)
    for label, where, args in [("FORWARD (out-of-sample, the real test)", " AND game_date>=?", (EPOCH,)),
                               ("in-sample seed (the 2-wk backtest, for reference)", " AND game_date<?", (EPOCH,))]:
        print(f"\n=== MLB pitcher-prop PAPER edges — {label} ===")
        for rule, book in [("k_over", "pinn"), ("k_over", "fd"),
                           ("outs_under", "pinn"), ("outs_under", "fd")]:
            n, w, l, pnl = _bucket(con, rule, book, where, args)
            openn = con.execute(f"SELECT COUNT(*) FROM paper WHERE rule=? AND book=? AND result IS NULL"
                                f"{where}", (rule, book, *args)).fetchone()[0]
            roi = pnl / n * 100 if n else 0
            hit = w / n * 100 if n else 0
            print(f"  {rule:11} @ {book:4}  {w}-{l}  ({hit:.0f}%)  {pnl:+.2f}u  ROI {roi:+.1f}%   [{openn} open]")
    # per-line slice for outs (is 15.5 really the sweet spot going forward?)
    print("\n  outs_under by line (pinn, all):", end=" ")
    for line, n, w, pnl in con.execute(
            "SELECT line, COUNT(*), SUM(result='W'), COALESCE(SUM(pnl),0) FROM paper "
            "WHERE rule='outs_under' AND book='pinn' AND result IN ('W','L') GROUP BY line ORDER BY line"):
        print(f"{line}:{w}/{n}({pnl:+.1f}u)", end="  ")
    # ★ the DIAMOND: outs-under is really an AWAY-starter effect (away go ~0.43 outs shorter, t≈2.8).
    # Track the home/away split forward — away should keep winning, home should keep losing.
    print("\n  outs_under home/away (pinn, all):", end=" ")
    for lbl, hv in (("AWAY", 0), ("HOME", 1)):
        g = con.execute("SELECT COUNT(*), SUM(result='W'), COALESCE(SUM(pnl),0) FROM paper "
                        "WHERE rule='outs_under' AND book='pinn' AND result IN ('W','L') AND home=?",
                        (hv,)).fetchone()
        n, w, pnl = g[0], g[1] or 0, g[2] or 0
        roi = pnl / n * 100 if n else 0
        print(f"{lbl} {w}/{n} ({roi:+.0f}%)", end="   ")
    # ★ stacked: AWAY + CONTACT offense (opp K% < CONTACT_MAX) — the sharpest slice
    print("\n  outs_under AWAY x offense (pinn):", end=" ")
    for lbl, cond in (("AWAY+CONTACT", f"opp_k < {CONTACT_MAX}"), ("AWAY+whiff", f"opp_k >= {CONTACT_MAX}")):
        g = con.execute(f"SELECT COUNT(*), SUM(result='W'), COALESCE(SUM(pnl),0) FROM paper "
                        f"WHERE rule='outs_under' AND book='pinn' AND result IN ('W','L') "
                        f"AND home=0 AND opp_k IS NOT NULL AND {cond}").fetchone()
        n, w, pnl = g[0], g[1] or 0, g[2] or 0
        roi = pnl / n * 100 if n else 0
        print(f"{lbl} {w}/{n} ({roi:+.0f}%)", end="   ")
    print()
    con.close()


if __name__ == "__main__":
    flag()
    grade()
    report()
