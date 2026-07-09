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

import requests

import wnba_tonight as T
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
SEEN = HERE / "wnba_notified.txt"


def _short(name):
    p = name.split()
    return f"{p[0][0]}.{p[-1]}" if len(p) >= 2 else name


def collect():
    pl = W.players()
    playing = T.tonight_teams()
    inj = T.injuries()
    alerts = []
    for name, status in inj.items():
        p = pl.get(name)
        if not p or p["team"] not in playing or p["min"] < 20 or status not in ("Out", "Doubtful"):
            continue
        try:
            tlog = W.game_log(p["id"])
        except RuntimeError:
            continue
        team_pl = {n: v for n, v in pl.items()
                   if v["team"] == p["team"] and n != name and v["gp"] >= 5}
        for n, v in team_pl.items():
            try:
                w = W.wowy(W.game_log(v["id"]), tlog)
            except RuntimeError:
                continue
            if w["n_without"] < 2:
                continue
            proj = w["without"]["min"]["mean"]
            blog = W.game_log(v["id"])
            for ev, stat, line, dec, hit, ns, _fga in T.prop_edges(n, blog, proj):
                key = f"{name}|{n}|{stat}|{line}"
                alerts.append((ev, key,
                    f"{_short(name)} OUT -> {_short(n)} {stat[:3]} o{line:g} "
                    f"{T._am(dec)} ({hit*100:.0f}% in {ns} role gms, +{ev*100:.0f}% est)"))
            dd = T.double_double_rate(blog, proj)
            if dd and dd[0] >= 0.40:                     # strong lagging-market DD candidate
                alerts.append((dd[0] - 0.5, f"{name}|{n}|dd",
                    f"{_short(name)} OUT -> {_short(n)} DOUBLE-DOUBLE {dd[0]*100:.0f}% in "
                    f"{dd[1]} role gms — check DD price (backup bigs lag)"))
    return sorted(alerts, reverse=True)


def main():
    alerts = collect()
    seen = set(SEEN.read_text().splitlines()) if SEEN.exists() else set()
    fresh = [(ev, k, msg) for ev, k, msg in alerts if k not in seen]
    print(f"wnba: {len(alerts)} +EV spots, {len(fresh)} new")
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
