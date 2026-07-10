"""WNBA play-by-play rotation engine — per-quarter minutes + on-court lineups.

ESPN's box score gives only TOTAL minutes. This parses the play-by-play substitution
stream into every player's on/off intervals, which unlocks:
  · minutes_by_period(gid)  -> {pid: [Q1, Q2, Q3, Q4, OT..]} minutes
  · co_minutes(gid)         -> {pid: {teammate_pid: minutes SHARED on the floor}}
The co-minutes are the deep signal for context-aware WOWY: a beneficiary's rebounds
depend not on whether the other big merely dressed, but on how long they were ON THE FLOOR
TOGETHER competing for the same boards.

Cached per game (a final game's PBP never changes).

    python wnba_pbp.py <game_id>
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "wnba_pbp_cache"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary?event={}"


def _sess():
    from curl_cffi import requests as cr
    return cr.Session(impersonate="chrome")


def _plen(period):
    return 600 if period <= 4 else 300           # 10-min quarters, 5-min OT


def _abs_time(period, clock_disp):
    """Absolute elapsed game-seconds at (period, clock-remaining 'M:SS')."""
    try:
        m, s = clock_disp.split(":")
        remaining = int(m) * 60 + int(float(s))
    except (ValueError, AttributeError):
        remaining = 0
    before = sum(_plen(p) for p in range(1, period))
    return before + (_plen(period) - remaining)


def fetch(game_id, max_age_days=3650):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{game_id}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except ValueError:
            pass
    j = _sess().get(SUMMARY.format(game_id), timeout=25).json()
    # only cache FINAL games (in-progress PBP would be partial)
    state = (j.get("header", {}).get("competitions", [{}])[0]
             .get("status", {}).get("type", {}).get("state"))
    slim = {"plays": j.get("plays", []), "boxscore": j.get("boxscore", {}), "state": state}
    if state == "post":
        f.write_text(json.dumps(slim))
    return slim


def intervals(game_id):
    """{pid: [(on_sec, off_sec), ...]} on-court intervals. Starters begin at 0; the game ends
    at the last play's timestamp. Robust to the ESPN participants order via a total-minutes
    check against the box score (swaps in/out if the naive order disagrees)."""
    j = fetch(game_id)
    plays = j.get("plays", [])
    if not plays:
        return {}
    box = j.get("boxscore", {})
    starters, box_min = set(), {}
    for tm in box.get("players", []):
        for stt in tm.get("statistics", []):
            keys = stt.get("keys") or []
            mi = keys.index("minutes") if "minutes" in keys else None
            for a in stt.get("athletes", []):
                pid = a.get("athlete", {}).get("id")
                if not pid:
                    continue
                if a.get("starter"):
                    starters.add(pid)
                if mi is not None:
                    try:
                        box_min[pid] = float((a.get("stats") or [0])[mi] or 0)
                    except (ValueError, IndexError):
                        pass
    end = max(_abs_time(p["period"]["number"], p["clock"]["displayValue"])
              for p in plays if p.get("clock"))

    def build(enter_idx):
        on = {pid: 0.0 for pid in starters}          # pid -> time came on (currently on)
        iv = defaultdict(list)
        for p in plays:
            if p.get("type", {}).get("text") != "Substitution":
                continue
            parts = [a.get("athlete", {}).get("id") for a in p.get("participants", [])]
            if len(parts) != 2:
                continue
            t = _abs_time(p["period"]["number"], p["clock"]["displayValue"])
            pin, pout = (parts[enter_idx], parts[1 - enter_idx])
            if pout in on:                             # close the exiting player's stint
                iv[pout].append((on.pop(pout), t))
            on[pin] = t                                # entering player comes on
        for pid, t0 in on.items():
            iv[pid].append((t0, end))
        return iv

    # pick the participants-order interpretation whose minutes best match the box score
    def err(iv):
        e = 0.0
        for pid, m in box_min.items():
            played = sum(b - a for a, b in iv.get(pid, [])) / 60.0
            e += abs(played - m)
        return e
    a, b = build(0), build(1)
    return a if err(a) <= err(b) else b


def minutes_by_period(game_id):
    """{pid: [Q1,Q2,Q3,Q4,(OT..)] minutes} from the on-court intervals."""
    iv = intervals(game_id)
    bounds = []
    t = 0
    for p in range(1, 8):
        bounds.append((t, t + _plen(p)))
        t += _plen(p)
    out = {}
    for pid, segs in iv.items():
        per = []
        for (lo, hi) in bounds:
            sec = sum(max(0, min(b, hi) - max(a, lo)) for a, b in segs)
            per.append(round(sec / 60.0, 1))
        while per and per[-1] == 0:
            per.pop()
        out[pid] = per
    return out


def co_minutes(game_id, pid):
    """{teammate_pid: minutes SHARED on the floor with `pid`} — the overlap of their stints."""
    iv = intervals(game_id)
    mine = iv.get(pid, [])
    out = {}
    for other, segs in iv.items():
        if other == pid:
            continue
        shared = sum(max(0, min(b, d) - max(a, c)) for a, b in mine for c, d in segs)
        if shared > 0:
            out[other] = round(shared / 60.0, 1)
    return out


if __name__ == "__main__":
    import sys
    gid = sys.argv[1] if len(sys.argv) > 1 else "401857050"
    mp = minutes_by_period(gid)
    print(f"per-quarter minutes ({len(mp)} players):")
    for pid, per in list(mp.items())[:6]:
        print(f"  {pid}: {per}  (total {sum(per):.0f})")
