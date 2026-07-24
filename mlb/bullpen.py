"""Bullpen availability — how hard the STARTER'S OWN pen was worked in the days before his start.

Mechanism (the hook decision, stated directly): a manager who burned his pen the last two or three
nights has nobody warm he trusts, so he RIDES the starter an extra inning — bad for an outs-under.
A rested pen (light usage, or an off-day) means the quick hook we're betting on. This is the one
lever that acts on the actual decision-maker rather than on pitcher quality, which the book prices.

WHY THIS ONE IS DIFFERENT FROM ppa/OBP: it is POINT-IN-TIME CLEAN. Everything here happened strictly
BEFORE first pitch, so a backtest of it isn't hindsight-flattered the way season team aggregates are
(that leakage is what inflated route B and opponent OBP, and both died at real lines).

Everything is derived from statsapi endpoints already in use. Per-game relief summaries are cached to
disk (the raw boxscores are ~1MB each and we only need three numbers per side).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import data

CACHE = Path(__file__).resolve().parent.parent / "mlb_pen_cache.json"
_mem: dict | None = None


def _cache() -> dict:
    global _mem
    if _mem is None:
        try:
            _mem = json.loads(CACHE.read_text())
        except Exception:
            _mem = {}
    return _mem


def _save() -> None:
    try:
        CACHE.write_text(json.dumps(_cache()))
    except Exception:
        pass


def game_relief(game_pk: int) -> dict:
    """{team_id: {"relief_pitches", "relievers", "starter_outs", "starter_pitches"}} for one game.
    A pitcher counts as RELIEF when statsapi says gamesStarted == 0 — never an outs/pitches proxy
    (that heuristic is what wrecked the r5 baseline once already)."""
    key = str(game_pk)
    c = _cache()
    if key in c:
        return c[key]
    out: dict = {}
    try:
        bx = data._get(f"/game/{game_pk}/boxscore")
    except Exception:
        return {}
    for side in ("home", "away"):
        t = (bx.get("teams") or {}).get(side) or {}
        tid = ((t.get("team") or {}).get("id"))
        if tid is None:
            continue
        players = t.get("players") or {}
        rp, rn, so, sp = 0, 0, 0, 0
        for pid in t.get("pitchers") or []:
            st = ((players.get(f"ID{pid}") or {}).get("stats") or {}).get("pitching") or {}
            n = st.get("numberOfPitches") or 0
            if (st.get("gamesStarted") or 0):
                so += st.get("outs") or 0
                sp += n
            else:
                rp += n
                rn += 1
        out[str(tid)] = {"relief_pitches": rp, "relievers": rn,
                         "starter_outs": so, "starter_pitches": sp}
    c[key] = out
    _save()
    return out


def team_of(game_pk: int, is_home: bool | None = None, opp_id: int | None = None) -> int | None:
    """The starter's OWN team id. The pitcher gamelog carries opp_id + is_home but not own team —
    this is the one hop that had this feature parked as 'deferred'.

    Prefer opp_id: the cached relief summary already holds BOTH team ids, so 'the side that isn't the
    opponent' resolves with no extra fetch. is_home is the fallback (costs one boxscore call)."""
    g = game_relief(game_pk)
    if not g:
        return None
    if opp_id is not None:
        other = [int(k) for k in g if int(k) != int(opp_id)]
        if len(other) == 1:
            return other[0]
    try:
        bx = data._get(f"/game/{game_pk}/boxscore")
        side = "home" if is_home else "away"
        return ((bx.get("teams") or {}).get(side) or {}).get("team", {}).get("id")
    except Exception:
        return None


def _sched(team_id: int, start: str, end: str) -> list[tuple[str, int]]:
    try:
        d = data._get("/schedule", sportId=1, teamId=team_id, startDate=start, endDate=end)
    except Exception:
        return []
    out = []
    for day in d.get("dates") or []:
        for g in day.get("games") or []:
            if (g.get("status") or {}).get("abstractGameState") == "Final":
                out.append((day.get("date"), g.get("gamePk")))
    return out


def availability(team_id: int, game_date: str, days: int = 3) -> dict:
    """Pen workload in the `days` calendar days BEFORE game_date (exclusive).

    Returns raw totals plus the 1- and 2-day slices. Raw totals are deliberate: an off-day
    contributes zero, so 'rested' and 'lightly used' both read low — which is exactly the
    direction the mechanism cares about."""
    try:
        d0 = dt.date.fromisoformat(game_date)
    except (TypeError, ValueError):
        return {}
    start, end = (d0 - dt.timedelta(days=days)).isoformat(), (d0 - dt.timedelta(days=1)).isoformat()
    games = _sched(team_id, start, end)
    per_day: dict[str, int] = {}
    rel_n = 0
    for gdate, pk in games:
        info = (game_relief(pk) or {}).get(str(team_id))
        if not info:
            continue
        per_day[gdate] = per_day.get(gdate, 0) + (info.get("relief_pitches") or 0)
        rel_n += info.get("relievers") or 0

    def _win(n: int) -> int:
        lo = (d0 - dt.timedelta(days=n)).isoformat()
        return sum(v for k, v in per_day.items() if k >= lo)

    ng = len(per_day)
    return {"bp_pitches": _win(days), "bp_pitches_2d": _win(2), "bp_pitches_1d": _win(1),
            "bp_relievers": rel_n, "bp_games": ng,
            "bp_per_game": round(_win(days) / ng, 1) if ng else None,
            "bp_offday": int(ng < days)}


def for_start(game_pk: int, is_home: bool | None, game_date: str, days: int = 3,
              opp_id: int | None = None) -> dict:
    """Convenience: pen availability for the team that started `game_pk`."""
    tid = team_of(game_pk, is_home, opp_id)
    if tid is None:
        return {}
    out = availability(tid, game_date, days)
    out["team_id"] = tid
    return out
