"""WNBA props phone alert — the hands-off trigger.

Runs the tonight board (injuries -> WOWY beneficiaries -> elevated-role prop edges) and
pushes the flagged +EV spots to ntfy, deduped so each fires once. Meant to run a few
times through the afternoon/evening on GitHub Actions — the last run before tip catches
confirmed lineups, which is the speed window the whole edge lives in.

Line:  WNBA A.Wilson OUT -> J.Loyd pts o12.5 -104 (67% in 9 role games, +18% est)

    NTFY_TOPIC=xxx python wnba_alert.py
"""
from __future__ import annotations

import datetime
import json
import os
import statistics as st
from collections import defaultdict
from pathlib import Path

import requests

import rotowire as RW
import wnba_context as CTX
import wnba_ledger as L
import wnba_proj_log as PL
import wnba_tonight as T
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
SEEN = HERE / "wnba_notified.txt"


def _short(name):
    p = name.split()
    return f"{p[0][0]}.{p[-1]}" if len(p) >= 2 else name


# Positional role groups — a vacated role goes mostly to SAME-position players. A guard
# (Clark) sitting hands minutes/usage to other GUARDS, not to a forward (Billings). So we
# scale a beneficiary's projected elevation by how well their position matches the out
# player's. This is the fix for flagging Billings (F) hard off Clark (G) — she was never a
# real beneficiary of a guard's absence (tonight the guards Hull/Harris cashed, she didn't).
_POSG = {"G": "G", "PG": "G", "SG": "G", "GF": "G", "F": "F", "SF": "F", "PF": "F",
         "FC": "C", "C": "C", "CF": "C"}
_COMPAT = {("G", "G"): 1.0, ("F", "F"): 1.0, ("C", "C"): 1.0,
           ("F", "C"): 0.6, ("C", "F"): 0.6, ("G", "F"): 0.3, ("F", "G"): 0.3,
           ("G", "C"): 0.15, ("C", "G"): 0.15}


def position_compat(bene_pos, out_positions):
    """1.0 = same role as an out player (a real beneficiary), down to 0.15 = opposite (a
    guard's absence barely elevates a center). Best match across the out players."""
    bg = _POSG.get((bene_pos or "F").upper(), "F")
    return max((_COMPAT.get((bg, _POSG.get((op or "F").upper(), "F")), 0.4)
                for op in out_positions), default=0.5)


def collect():
    """Returns (alerts, preds): alerts are ntfy message tuples, preds are the full
    prediction rows logged to the ledger so every flagged spot gets graded and fed back
    into the model."""
    pl = W.players()
    playing = T.tonight_teams()
    matchups = T.tonight_matchups()
    inj = T.injuries()
    # ET slate date, NOT UTC — else a game tipping ~02:00Z (10pm ET) gets logged under two
    # different UTC dates across midnight and the SAME bets double-count in the tracker.
    today = datetime.datetime.now(T.ET).date().isoformat()
    lines, rates = CTX.game_lines(), CTX.team_rates()    # Vegas total + pace, fetched once
    gids = T.game_ids()                                  # team -> game id (for lineup lookup)
    # truly out = listed out AND no fresh posted props (a book posting a slate = playing)
    out_names = {n for n, s in inj.items() if s in ("Out", "Doubtful") and not T.playing_now(n)}
    # group tonight's genuine key outs BY TEAM — a beneficiary gets ONE projection off the
    # COMBINED absence (the user's edge: 2+ impact players out compounds the boost), not a
    # duplicate per out-player.
    outs_by_team = defaultdict(list)
    for name, status in inj.items():
        p = pl.get(name)
        if (p and p["team"] in playing and p["min"] >= 20 and status in ("Out", "Doubtful")
                and not T.playing_now(name)):
            outs_by_team[p["team"]].append((name, p))

    alerts, preds, proj_rows = [], [], []
    log_cache = {}                                   # fetch each player's game log at most once

    def glog(pid):
        if pid not in log_cache:
            try:
                log_cache[pid] = W.game_log(pid)
            except Exception:
                log_cache[pid] = []
        return log_cache[pid]

    for team, outs in outs_by_team.items():
        out_logs = [glog(p["id"]) for _, p in outs]
        if not all(out_logs):
            continue
        out_label = "+".join(_short(nm) for nm, _ in outs)      # "C.Clark+A.Boston"
        out_full = ", ".join(nm for nm, _ in outs)
        # combined vacated pool = all the out players' production is up for grabs tonight
        vacated = {"points": sum(p["pts"] for _, p in outs),
                   "rebounds": sum(p["reb"] for _, p in outs),
                   "assists": sum(p["ast"] for _, p in outs)}
        ctx = CTX.matchup_context(team, matchups.get(team, ""), lines, rates)
        env = []
        if ctx["total"]:
            env.append(f"O/U{ctx['total']:g}")
        if ctx["pace_vs_lg"] is not None and abs(ctx["pace_vs_lg"]) > 2:
            env.append("fast" if ctx["pace_vs_lg"] > 0 else "slow")
        env_tag = " · " + " ".join(env) if env else ""
        starters = T.game_starters(gids.get(team))       # None until the lineup posts
        # tonight's lineup CONTEXT for the context-weighted projection: date->minutes maps for
        # the out stars (target 0) + the team's RotoWire starters (competitors, target = their
        # expected minutes). Built once per team; degrades to no-context if RotoWire is thin.
        out_dm = [{g["date"][:10]: g.get("min", 0) for g in ol} for ol in out_logs]
        id_by_norm = {RW.norm(nm): vv["id"] for nm, vv in pl.items()}
        team_mates = []
        for t in T.rw_lineups():
            if t["team"] != team:
                continue
            for p_pos, p_nm, inj in t["starters"]:
                pid = id_by_norm.get(RW.norm(p_nm))
                if inj or not pid:
                    continue
                lg = glog(pid)
                mm = [g["min"] for g in lg[-10:] if g["min"] > 8]
                if mm:
                    team_mates.append((RW.norm(p_nm), T._PG.get(p_pos, "F"),
                                       {g["date"][:10]: g["min"] for g in lg}, st.median(mm)))
        team_pl = {n: v for n, v in pl.items()
                   if v["team"] == team and n not in out_names and v["gp"] >= 5}
        for n, v in team_pl.items():
            blog = glog(v["id"])
            if not blog:
                continue
            # combined absence (all outs sitting together) — the compounded boost
            w = W.wowy_multi(blog, out_logs)
            if w["n_without"] < 2 and len(outs) > 1:
                # too few games with ALL out together -> best single-out split as the proxy
                cands = [(W.wowy(blog, ol), nm) for (nm, _), ol in zip(outs, out_logs)]
                w = max(cands, key=lambda x: x[0]["n_without"])[0]
            if w["n_without"] < 2:
                continue
            # POSITION MATCH (minutes): a vacated role's MINUTES go to same-position players,
            # so scale the projected minutes-elevation by positional fit — a forward barely
            # inherits a guard's minutes. But DON'T hard-drop cross-position beneficiaries: a
            # high-usage / rebounding guard (Clark) still vacates shots + boards that reach the
            # forwards on the floor. That production flows through the minutes-honest projection
            # + the vacated pool, not a binary position gate.
            pw = position_compat(v.get("position"), [op.get("position") for _, op in outs])
            with_min = w["with"]["min"]["mean"]
            proj = with_min + pw * (w["without"]["min"]["mean"] - with_min)
            # RECENCY: the WOWY split averages OLD without-them games, so it lags a player whose
            # role is actively expanding (Allemand: split says 30, she's played 35 the last 3).
            # Credit the higher of the role estimate and recent minutes — only lifts ascending
            # players, never lowers anyone. (Fixes the live-model stale-minutes gap; the backtest
            # baseline already used trailing-5 minutes, so this closes LIVE up to that level.)
            recent5 = [g["min"] for g in blog[:5] if g["min"] > 8]   # game_log is NEWEST-first
            if recent5:
                proj = max(proj, st.median(recent5))
            if proj - with_min <= 0.3 and pw < 0.6:        # no minutes bump AND no role overlap
                continue
            conf = T.starter_label(n, team, starters, proj)  # RotoWire-first confirmed/likely/bench
            # PROJECTION TRACKER: log this beneficiary's FULL projection (min + pts/reb/ast +
            # assumptions) whether or not any prop flags — the background learner grades it later.
            pa = T.project_all(blog, proj)
            prow = None
            if pa:
                prow = {"date": today, "pid": v["id"], "player": n, "team": team,
                        "opp": matchups.get(team, ""), "out_player": out_full,
                        "confidence": conf, "pos": v.get("position"), "flagged": 0,
                        "d_min": round(w["without"]["min"]["mean"]
                                       - w["with"]["min"]["mean"], 1), **pa}
                proj_rows.append(prow)
            n_preds0 = len(preds)                            # to mark whether this player got a bet
            mates_n = [(pg, dm, em) for (nm, pg, dm, em) in team_mates if nm != RW.norm(n)]
            for e in T.prop_edges(n, blog, proj, w, vacated, ctx, out_logs=out_dm, mates=mates_n,
                                  opp=matchups.get(team, ""), pos=v.get("position")):
                # beneficiary+stat+line, dated (re-fires next slate)
                key = f"{today}|{n}|{e['stat']}|{e['line']}"
                tag = " [stale line]" if e["stale"] else ""
                # 2-day backtest tell: EV >40% flags went 0-4 — implausibly-fat EV on a
                # mainstream line = a thin-sample over-projection, not real. Warn on it.
                if e["ev"] > 0.35 or e["n"] < 6:
                    tag += " [thin-sample, be skeptical]"
                # per-stat driver: points shows FGA (+FTA/3PA), rebounds reb, assists ast
                dl = {"points": "FGA", "rebounds": "reb", "assists": "ast"}[e["stat"]]
                bits = [f"{dl} {e['driver']:+g}" if e["driver"] is not None else "",
                        f"min {e['d_min']:+g}" if e["d_min"] is not None else ""]
                if e["stat"] == "points" and e["d_fta"] is not None:
                    bits += [f"FTA {e['d_fta']:+g}", f"3PA {e['d_3pa']:+g}"]
                wo = " | w/o: " + ", ".join(b for b in bits if b) if any(bits) else ""
                hits = round(e["hit"] * e["n"])           # bet-side record in the role games
                rec = f"{hits}-{e['n']-hits}"
                ctag = {"confirmed": " ✓STARTING", "bench": " ⚠NOT STARTING",
                        "likely": " (likely starts)", "projected": " (lineup TBD)"}[conf]
                sd = "o" if e["side"] == "over" else "u"   # over/under prefix on the line
                alerts.append((e["ev"], key,
                    f"{out_label} OUT -> {_short(n)} {e['stat'][:3]} {sd}{e['line']:g} "
                    f"{T._am(e['dec'])}{wo} | {rec} {e['hit']*100:.0f}% "
                    f"| proj {e['elev_avg']:g} +{e['ev']*100:.0f}%EV{ctag}{tag}{env_tag}"))
                preds.append({"pred_date": today, "out_player": out_full, "player": n,
                              "team": team, "opp": matchups.get(team, ""),
                              "stat": e["stat"], "line": e["line"], "odds": e["dec"],
                              "book": "fd", "proj_hit": round(e["hit"], 3), "side": e["side"],
                              "season_avg": e["season_avg"], "elev_avg": e["elev_avg"],
                              "proj_min": round(proj, 1), "n_elev": e["n"],
                              "ev": round(e["ev"], 3), "stale": int(e["stale"]),
                              "d_stat": e["d_stat"], "d_fga": e["d_fga"], "d_min": e["d_min"],
                              "driver": e["driver"], "vac": e["vac"],
                              "total": e["total"], "pace": e["pace"], "opp_def": e["opp_def"],
                              "d_fta": e["d_fta"], "d_3pa": e["d_3pa"],
                              "basis": e["basis"], "samples": json.dumps(e["samples"]),
                              "confidence": conf})
            if prow is not None:                            # did any prop for this player flag a bet?
                prow["flagged"] = 1 if len(preds) > n_preds0 else 0
            dd = T.double_double_rate(blog, proj, w)
            if dd and dd["rate"] >= 0.40:                # strong lagging-market DD candidate
                bits = [f"reb {dd['d_reb']:+g}" if dd["d_reb"] is not None else "",
                        f"pts {dd['d_pts']:+g}" if dd["d_pts"] is not None else "",
                        f"min {dd['d_min']:+g}" if dd["d_min"] is not None else ""]
                wo = " | w/o: " + ", ".join(b for b in bits if b) if any(bits) else ""
                alerts.append((dd["rate"] - 0.5, f"{today}|{n}|dd",
                    f"{out_label} OUT -> {_short(n)} DOUBLE-DOUBLE {dd['rate']*100:.0f}% in "
                    f"{dd['n']} role gms{wo} — check DD price (backup bigs lag)"))
    PL.log(proj_rows)                       # background projection tracker (learning loop)
    return sorted(alerts, reverse=True), preds


def main():
    alerts, preds = collect()
    logged = L.log_predictions(preds)                    # feed the learning loop
    seen = set(SEEN.read_text().splitlines()) if SEEN.exists() else set()
    fresh, seen_this_run = [], set()
    for ev, k, msg in alerts:                             # alerts sorted by EV desc
        if k in seen or k in seen_this_run:               # collapse same-spot duplicates
            continue
        seen_this_run.add(k)
        fresh.append((ev, k, msg))
    print(f"wnba: {len(alerts)} +EV spots, {len(fresh)} new, {logged} logged to ledger")
    for _ev, _k, msg in fresh:
        print("  " + msg)
    topic = os.environ.get("NTFY_TOPIC")
    if topic and fresh:
        body = "\n".join(m for _e, _k, m in fresh[:20])
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers={"Title": "WNBA prop spots (injury-driven)",
                                   "Priority": "high", "Tags": "basketball"}, timeout=15)
            print("pushed")
        except requests.RequestException as e:
            print("push failed:", e)
    for _e, k, _m in fresh:
        seen.add(k)
    SEEN.write_text("\n".join(sorted(seen)[-2000:]))


if __name__ == "__main__":
    main()
