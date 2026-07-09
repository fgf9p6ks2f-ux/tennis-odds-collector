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
from collections import defaultdict
from pathlib import Path

import requests

import wnba_context as CTX
import wnba_ledger as L
import wnba_tonight as T
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
SEEN = HERE / "wnba_notified.txt"


def _short(name):
    p = name.split()
    return f"{p[0][0]}.{p[-1]}" if len(p) >= 2 else name


def collect():
    """Returns (alerts, preds): alerts are ntfy message tuples, preds are the full
    prediction rows logged to the ledger so every flagged spot gets graded and fed back
    into the model."""
    pl = W.players()
    playing = T.tonight_teams()
    matchups = T.tonight_matchups()
    inj = T.injuries()
    today = datetime.date.today().isoformat()
    lines, rates = CTX.game_lines(), CTX.team_rates()    # Vegas total + pace, fetched once
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

    alerts, preds = [], []
    for team, outs in outs_by_team.items():
        try:
            out_logs = [W.game_log(p["id"]) for _, p in outs]
        except RuntimeError:
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
        team_pl = {n: v for n, v in pl.items()
                   if v["team"] == team and n not in out_names and v["gp"] >= 5}
        for n, v in team_pl.items():
            try:
                blog = W.game_log(v["id"])
            except RuntimeError:
                continue
            # combined absence (all outs sitting together) — the compounded boost
            w = W.wowy_multi(blog, out_logs)
            if w["n_without"] < 2 and len(outs) > 1:
                # too few games with ALL out together -> best single-out split as the proxy
                cands = [(W.wowy(blog, ol), nm) for (nm, _), ol in zip(outs, out_logs)]
                w = max(cands, key=lambda x: x[0]["n_without"])[0]
            if w["n_without"] < 2:
                continue
            proj = w["without"]["min"]["mean"]
            if proj - w["with"]["min"]["mean"] <= 0:     # genuine beneficiary: plays MORE
                continue
            for e in T.prop_edges(n, blog, proj, w, vacated, ctx):
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
                overs = round(e["hit"] * e["n"])          # elevated-games record
                rec = f"{overs}-{e['n']-overs}"
                alerts.append((e["ev"], key,
                    f"{out_label} OUT -> {_short(n)} {e['stat'][:3]} o{e['line']:g} "
                    f"{T._am(e['dec'])}{wo} | {rec} {e['hit']*100:.0f}% "
                    f"| elev {e['elev_avg']:g} +{e['ev']*100:.0f}%EV{tag}{env_tag}"))
                preds.append({"pred_date": today, "out_player": out_full, "player": n,
                              "team": team, "opp": matchups.get(team, ""),
                              "stat": e["stat"], "line": e["line"], "odds": e["dec"],
                              "book": "fd", "proj_hit": round(e["hit"], 3),
                              "season_avg": e["season_avg"], "elev_avg": e["elev_avg"],
                              "proj_min": round(proj, 1), "n_elev": e["n"],
                              "ev": round(e["ev"], 3), "stale": int(e["stale"]),
                              "d_stat": e["d_stat"], "d_fga": e["d_fga"], "d_min": e["d_min"],
                              "driver": e["driver"], "vac": e["vac"],
                              "total": e["total"], "pace": e["pace"], "opp_def": e["opp_def"],
                              "d_fta": e["d_fta"], "d_3pa": e["d_3pa"],
                              "basis": e["basis"], "samples": json.dumps(e["samples"])})
            dd = T.double_double_rate(blog, proj, w)
            if dd and dd["rate"] >= 0.40:                # strong lagging-market DD candidate
                bits = [f"reb {dd['d_reb']:+g}" if dd["d_reb"] is not None else "",
                        f"pts {dd['d_pts']:+g}" if dd["d_pts"] is not None else "",
                        f"min {dd['d_min']:+g}" if dd["d_min"] is not None else ""]
                wo = " | w/o: " + ", ".join(b for b in bits if b) if any(bits) else ""
                alerts.append((dd["rate"] - 0.5, f"{today}|{n}|dd",
                    f"{out_label} OUT -> {_short(n)} DOUBLE-DOUBLE {dd['rate']*100:.0f}% in "
                    f"{dd['n']} role gms{wo} — check DD price (backup bigs lag)"))
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
