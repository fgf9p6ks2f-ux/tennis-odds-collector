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
import wnba_news as NEWS
import wnba_tonight as T
import wnba_wowy as W

HERE = Path(__file__).resolve().parent
STATE = HERE / "wnba_injury_state.json"        # last-seen key-out statuses (for diffing)
Q_STATE = HERE / "wnba_question_state.json"    # last-seen key QUESTIONABLE/GTD statuses (for diffing)
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


def key_q(playing, inj, pl):
    """{name: 'Questionable'} for key (>=20 mpg / >=10 ppg) players on today's slate who are
    QUESTIONABLE — a NEW one triggers an early watchlist scan before they resolve to Out (which
    is when the line moves). RotoWire GTD is folded in upstream by questionable_stars."""
    return {n: "Questionable" for n, s in inj.items()
            if s == "Questionable" and n in pl and pl[n]["team"] in playing
            and (pl[n]["min"] >= KEY_MIN or pl[n]["pts"] >= 10)}


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
    # NEWS aggregator (3rd source, FASTEST): RotoWire's player-news blurbs + Google News post minutes
    # ahead of the formal ESPN injury status. Merge a news-detected OUT that references TODAY into the
    # injury dict -> the existing diff fires the scan + beneficiary flags + push sooner. Degrades silently.
    try:
        for it in NEWS.new_items(list(pl)):
            if (it["on_roster"] and it["status"] == "out" and NEWS.references_today(it["text"])
                    and inj.get(it["player"]) not in ("Out", "Doubtful")):
                inj[it["player"]] = "Out"
                print(f"news OUT (ahead of ESPN): {it['player']} — {it['text'][:70]}")
    except Exception as e:
        print("news aggregator skipped:", str(e)[:60])
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
    cur_q = key_q(playing, inj, pl)                               # questionable/GTD tier (the timing edge)
    prev_q = json.loads(Q_STATE.read_text()) if Q_STATE.exists() else {}
    Q_STATE.write_text(json.dumps(cur_q, indent=1, sort_keys=True))
    # CALIBRATION LOG: record every key questionable/doubtful/GTD player on tonight's slate (once, via
    # INSERT-OR-IGNORE) so wnba_question_log can resolve sit-vs-play later and recalibrate SIT_PROB.
    try:
        import wnba_question_log as QL
        today_et = dt.datetime.now(T.ET).date().isoformat()
        norm2name = {RW.norm(nn): nn for nn in pl}
        obs, seen = [], set()
        for nn, s in inj.items():                                 # ESPN Questionable / Doubtful
            if s in ("Questionable", "Doubtful") and nn in pl and pl[nn]["team"] in playing \
                    and (pl[nn]["min"] >= 20 or pl[nn]["pts"] >= 10):
                obs.append((nn, pl[nn]["team"], s, pl[nn]["min"]))
                seen.add(nn)
        for nnm in RW.questionable_players(board):                # RotoWire GTD
            full = norm2name.get(nnm)
            if full and full not in seen and pl[full]["team"] in playing \
                    and (pl[full]["min"] >= 20 or pl[full]["pts"] >= 10):
                obs.append((full, pl[full]["team"], "GTD", pl[full]["min"]))
        if obs:
            QL.record(today_et, obs)
    except Exception as e:
        print("question-log record skipped:", str(e)[:60])
    if first_run:
        # cold start: record the baseline, don't fire the whole current injury list as
        # "news". The 4x/day full-board alert covers already-known outs; this watcher
        # only ever pushes genuine deltas from here on.
        print(f"cold start — baselined {len(cur)} known outs, {len(cur_q)} questionable, no push")
        return
    # NEW = newly Out/Doubtful, or escalated Doubtful->Out, since the last poll. (An
    # Out->Doubtful downgrade is not news worth a push.)
    new = {n: s for n, s in cur.items()
           if prev.get(n) != s and not (prev.get(n) == "Out" and s == "Doubtful")}
    # newly QUESTIONABLE since the last poll -> fire the early watchlist scan (positions us on the
    # beneficiary before the star resolves to Out). Drop any that ALSO newly went Out (the firm
    # `new` path already covers those, at full urgency).
    new_q = {n: s for n, s in cur_q.items() if prev_q.get(n) != s and n not in new}
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
    if not new and not new_q:
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
    qnews = ", ".join(f"{A._short(n)} Q" for n in sorted(new_q))
    print(f"NEW: {news or '(none)'} · questionable: {qnews or '(none)'} — running scan")
    # FAST REPLACEMENT READ: the instant the news hits, name who likely slides into the role +
    # projected minutes (position + WOWY depth) — before RotoWire confirms / the line moves.
    # Only for firm OUTs — a questionable-only trigger produces no `new`, so this loop no-ops.
    repl = []
    try:
        pl_all = W.players()
        by_team = {}
        for on in new:
            v = pl_all.get(on)
            if v and v.get("team"):
                by_team.setdefault(v["team"], []).append(on)
        for team, outs in by_team.items():
            lu = DP.projected_lineup(team, outs, pl_all)
            # LEAD with usage ABSORPTION — the vacated SHOTS flow to EXISTING players (FGA-WOWY,
            # which is empirical so it catches small-ball automatically). This is the reliable,
            # bet-relevant signal: the bet keys on WHO shoots more, not on who fills the spot.
            for u in lu["usage_up"][:3]:
                repl.append(f"↳ {team}: {A._short(u['name'])} absorbs +{u['d_fga']:g} FGA "
                            f"(→{u['fga_wo']:g}/g w/o {A._short(u['vs'])})")
            # someone ALWAYS fills the starting SPOT, but our exact-starter guess is only ~0-20%
            # (top-3 ~60%) — so surface the SHORTLIST of likely fills, not one confident wrong name.
            for p in lu["promoted"]:
                cands = " / ".join(A._short(c) for c in p.get("candidates", [p["name"]]))
                repl.append(f"↳ {team}: {A._short(p['replaces'])}'s spot → likely {cands} "
                            f"(shortlist; exact starter ~coin-flip)")
    except Exception as e:
        print("lineup read skipped:", str(e)[:60])
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
        if new:                          # a firm OUT broke -> URGENT push, with the replacement read
            body = (f"🚨 {news}\n\n" + ("\n".join(repl) + "\n\n" if repl else "")
                    + A._notif_body(fresh))
            title, prio, tags = f"WNBA news: {news}"[:120], "urgent", "rotating_light"
        else:                            # questionable-only trigger -> softer early WATCH heads-up
            body = f"⏳ Questionable: {qnews}\n\n" + A._notif_body(fresh)
            title, prio, tags = f"WNBA watch: {qnews}"[:120], "high", "hourglass_flowing_sand"
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers={"Title": title, "Priority": prio, "Tags": tags}, timeout=15)
            print(f"pushed ({prio})")
        except requests.RequestException as e:
            print("push failed:", e)
    for _e, k, _m in fresh:
        seen.add(k)
    A.SEEN.write_text("\n".join(sorted(seen)[-2000:]))


if __name__ == "__main__":
    main()
