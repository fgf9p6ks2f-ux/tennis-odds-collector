"""WNBA injury watcher — the SPEED layer. Poll cheap, act only on change.

Beating the book to reprice after news IS the edge, so poll the ESPN injury feed often
(~0.4s) and diff it against the last snapshot. The full beneficiary+prop scan is slow
(rebuilds ~200 season lines, ~47s), so it runs ONLY when a key player on today's slate
NEWLY flips to Out/Doubtful — turning a 2-5 min cadence into cheap "this just broke"
detection instead of a wasteful re-scan every few minutes. The roster / season-average
map is cached to disk (refreshed every few hours) so both the poll and the triggered
scan skip the 47s rebuild.

Pairs with wnba_alert.py: that posts the full board a few times a day (the baseline);
this fires an URGENT push the moment something new drops. Shared notified-file dedupe
means the two never double-push the same spot.

    NTFY_TOPIC=xxx python wnba_watch.py       # one poll; scan + urgent push only on new outs
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import requests

import rotowire as RW
import wnba_alert as A
import wnba_depth as DP
import wnba_ledger as L
import wnba_tonight as T
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
STATE = HERE / "wnba_injury_state.json"        # last-seen key-out statuses (for diffing)
CONF_STATE = HERE / "wnba_confirm_state.json"  # last-seen set of CONFIRMED-lineup teams (for diffing)
PCACHE = HERE / "wnba_players_cache.json"      # roster + season averages (skip the 47s rebuild)
KEY_MIN = 20.0                                 # a player worth reacting to (mpg)


def players_cached(max_age_h=6):
    """Roster/season-avg map from disk if fresh, else rebuild + cache. Primes wnba_wowy's
    in-process cache too, so a triggered scan reuses it instead of re-fetching 200 logs."""
    now = dt.datetime.now(dt.timezone.utc)
    if PCACHE.exists():
        try:
            d = json.loads(PCACHE.read_text())
            age = (now - dt.datetime.fromisoformat(d["ts"])).total_seconds() / 3600
            if age < max_age_h and d.get("players"):
                W._PLAYERS_CACHE.update(d["players"])
                return d["players"]
        except (ValueError, KeyError):
            pass
    pl = W.players()
    PCACHE.write_text(json.dumps({"ts": now.isoformat(), "players": pl}))
    return pl


def key_outs(playing, inj, pl):
    """{name: status} for key (>=20 mpg) players on today's slate ruled Out/Doubtful AND
    with no fresh posted props (a book posting a slate means they're actually playing)."""
    return {n: s for n, s in inj.items()
            if s in ("Out", "Doubtful") and n in pl
            and pl[n]["team"] in playing and pl[n]["min"] >= KEY_MIN
            and not T.playing_now(n)}


def main():
    playing = T.tonight_teams()
    if not playing:
        print("no games on the slate — idle")
        return
    pl = players_cached()
    inj = T.injuries()
    # merge RotoWire's ruled-OUT list (mapped to full roster names via first-initial+lastname)
    # — a 2nd injury source that catches outs the ESPN feed is slow on and confirms them via
    # the actual posted lineup. Degrades silently if RotoWire is unreachable.
    board = []
    try:
        board = T.rw_lineups()
        rw_out = RW.out_players(board)
        for full in pl:
            if RW.norm(full) in rw_out and inj.get(full) not in ("Out", "Doubtful"):
                inj[full] = "Out"
    except Exception as e:
        print("rotowire merge skipped:", str(e)[:60])
    # confirmation diff: a team flipping projected->CONFIRMED near tip changes the lineup labels
    # (confirmed / bench) with NO new injury, so the dashboard must refresh even when `new` is empty.
    conf_sig = sorted(t["team"] for t in board if t.get("status") == "confirmed")
    prev_conf = json.loads(CONF_STATE.read_text()) if CONF_STATE.exists() else []
    CONF_STATE.write_text(json.dumps(conf_sig))
    conf_changed = bool(conf_sig) and conf_sig != prev_conf
    cur = key_outs(playing, inj, pl)
    first_run = not STATE.exists()
    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    STATE.write_text(json.dumps(cur, indent=1, sort_keys=True))   # deterministic -> no-op runs don't commit
    if first_run:
        # cold start: record the baseline, don't fire the whole current injury list as
        # "news". The 4x/day full-board alert covers already-known outs; this watcher
        # only ever pushes genuine deltas from here on.
        print(f"cold start — baselined {len(cur)} known outs, no push")
        return
    # NEW = newly Out/Doubtful, or escalated Doubtful->Out, since the last poll. (An
    # Out->Doubtful downgrade is not news worth a push.)
    new = {n: s for n, s in cur.items()
           if prev.get(n) != s and not (prev.get(n) == "Out" and s == "Doubtful")}
    # BACK = was a key out last poll, now gone (off the injury report / active / props posted)
    # -> the player RETURNED, so any beneficiary play off their absence is VOID. Push a warning.
    back = sorted(n for n in prev if n not in cur)
    topic = os.environ.get("NTFY_TOPIC")
    if back:
        bmsg = ", ".join(A._short(n) for n in back)
        print(f"BACK (off injury report): {bmsg} — beneficiary plays off their absence now void")
        if topic:
            try:
                requests.post(f"https://ntfy.sh/{topic}",
                    data=(f"⚠ BACK: {bmsg} — OFF the injury report / now active. Pull any "
                          f"beneficiary plays built on their absence.").encode("utf-8"),
                    headers={"Title": f"WNBA: {bmsg} back"[:120], "Priority": "high",
                             "Tags": "warning"}, timeout=15)
                print("pushed (back)")
            except requests.RequestException as e:
                print("back push failed:", e)
    if not new:
        if conf_changed:
            # lineups locked in (no new injury) — re-run the scan so the ledger's confidence
            # labels flip likely->confirmed/bench and the dashboard regenerates. No push.
            print(f"lineups confirmed ({len(conf_sig)} teams) — refreshing confidence, no push")
            _, preds = A.collect()
            L.log_predictions(preds)
        else:
            print(f"no new outs ({len(cur)} known: {', '.join(sorted(cur)) or 'none'})")
        return

    news = ", ".join(f"{A._short(n)} {s}" for n, s in sorted(new.items()))
    print(f"NEW: {news} — running scan")
    # FAST REPLACEMENT READ: the instant the news hits, name who likely slides into the role +
    # projected minutes (position + WOWY depth) — before RotoWire confirms / the line moves.
    repl = []
    try:
        pl_all = W.players()
        for on in new:
            v = pl_all.get(on)
            if not v:
                continue
            rot = DP.team_rotation(v.get("team"), pl_all)
            olog = [g for g in W.game_log(v["id"]) if g["min"] > 0]
            p = DP.primary(v["id"], v.get("position"), olog, rot)
            if p:
                repl.append(f"↳ {A._short(on)} out → {A._short(p['name'])} ~{p['proj_min']:g}min "
                            f"({p['pos']}{'✓' if p['confirmed'] else ''})")
    except Exception as e:
        print("depth read skipped:", str(e)[:60])
    for r in repl:
        print("  " + r)
    alerts, preds = A.collect()
    logged = L.log_predictions(preds)
    seen = set(A.SEEN.read_text().splitlines()) if A.SEEN.exists() else set()
    fresh, this_run = [], set()
    for ev, k, msg in alerts:                       # alerts sorted by EV desc
        if k in seen or k in this_run:
            continue
        this_run.add(k)
        fresh.append((ev, k, msg))
    print(f"wnba-watch: {len(alerts)} spots, {len(fresh)} new, {logged} logged to ledger")
    for _e, _k, m in fresh:
        print("  " + m)
    topic = os.environ.get("NTFY_TOPIC")
    if topic and fresh:
        body = (f"JUST IN: {news}\n" + ("\n".join(repl) + "\n" if repl else "")
                + "\n".join(m for _e, _k, m in fresh[:20]))
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers={"Title": f"WNBA news: {news}"[:120],
                                   "Priority": "urgent", "Tags": "rotating_light"}, timeout=15)
            print("pushed (urgent)")
        except requests.RequestException as e:
            print("push failed:", e)
    for _e, k, _m in fresh:
        seen.add(k)
    A.SEEN.write_text("\n".join(sorted(seen)[-2000:]))


if __name__ == "__main__":
    main()
