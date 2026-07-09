"""WNBA props phone alert — the hands-off trigger.

Runs the tonight board (injuries -> WOWY beneficiaries -> elevated-role prop edges) and
pushes the flagged +EV spots to ntfy, deduped so each fires once. Meant to run a few
times through the afternoon/evening on GitHub Actions — the last run before tip catches
confirmed lineups, which is the speed window the whole edge lives in.

Line:  WNBA A.Wilson OUT -> J.Loyd pts o12.5 -104 (67% in 9 role games, +18% est)

    NTFY_TOPIC=xxx python wnba_alert.py
"""
from __future__ import annotations

import os
from pathlib import Path

import datetime

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
    # players who are themselves out can't be beneficiaries of a teammate sitting
    out_names = {n for n, s in inj.items() if s in ("Out", "Doubtful")}
    alerts, preds = [], []
    for name, status in inj.items():
        p = pl.get(name)
        if not p or p["team"] not in playing or p["min"] < 20 or status not in ("Out", "Doubtful"):
            continue
        try:
            tlog = W.game_log(p["id"])
        except RuntimeError:
            continue
        team_pl = {n: v for n, v in pl.items()
                   if v["team"] == p["team"] and n != name and v["gp"] >= 5
                   and n not in out_names}
        for n, v in team_pl.items():
            try:
                blog = W.game_log(v["id"])
            except RuntimeError:
                continue
            w = W.wowy(blog, tlog)
            if w["n_without"] < 2:
                continue
            proj = w["without"]["min"]["mean"]
            if proj - w["with"]["min"]["mean"] <= 0:     # only genuine beneficiaries: must
                continue                                 # play MORE without the out player
            vacated = {"points": p["pts"], "rebounds": p["reb"], "assists": p["ast"]}
            ctx = CTX.matchup_context(p["team"], matchups.get(p["team"], ""), lines, rates)
            env = []
            if ctx["total"]:
                env.append(f"O/U{ctx['total']:g}")
            if ctx["pace_vs_lg"] is not None and abs(ctx["pace_vs_lg"]) > 2:
                env.append("fast" if ctx["pace_vs_lg"] > 0 else "slow")
            env_tag = " · " + " ".join(env) if env else ""
            for e in T.prop_edges(n, blog, proj, w, vacated, ctx):
                # beneficiary-centric + dated: dedups when several stars are out together
                # (same spot triggered by each), re-fires on the next day's slate
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
                alerts.append((e["ev"], key,
                    f"{_short(name)} OUT -> {_short(n)} {e['stat'][:3]} o{e['line']:g} "
                    f"{T._am(e['dec'])}{wo} | {e['hit']*100:.0f}%/{e['n']}g "
                    f"elev {e['elev_avg']:g} +{e['ev']*100:.0f}%EV{tag}{env_tag}"))
                preds.append({"pred_date": today, "out_player": name, "player": n,
                              "team": p["team"], "opp": matchups.get(p["team"], ""),
                              "stat": e["stat"], "line": e["line"], "odds": e["dec"],
                              "book": "fd", "proj_hit": round(e["hit"], 3),
                              "season_avg": e["season_avg"], "elev_avg": e["elev_avg"],
                              "proj_min": round(proj, 1), "n_elev": e["n"],
                              "ev": round(e["ev"], 3), "stale": int(e["stale"]),
                              "d_stat": e["d_stat"], "d_fga": e["d_fga"], "d_min": e["d_min"],
                              "driver": e["driver"], "vac": e["vac"],
                              "total": e["total"], "pace": e["pace"], "opp_def": e["opp_def"],
                              "d_fta": e["d_fta"], "d_3pa": e["d_3pa"]})
            dd = T.double_double_rate(blog, proj, w)
            if dd and dd["rate"] >= 0.40:                # strong lagging-market DD candidate
                bits = [f"reb {dd['d_reb']:+g}" if dd["d_reb"] is not None else "",
                        f"pts {dd['d_pts']:+g}" if dd["d_pts"] is not None else "",
                        f"min {dd['d_min']:+g}" if dd["d_min"] is not None else ""]
                wo = " | w/o: " + ", ".join(b for b in bits if b) if any(bits) else ""
                alerts.append((dd["rate"] - 0.5, f"{today}|{n}|dd",
                    f"{_short(name)} OUT -> {_short(n)} DOUBLE-DOUBLE {dd['rate']*100:.0f}% in "
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
