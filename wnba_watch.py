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
STATUS_STATE = HERE / "wnba_status_state.json" # last-seen key-player injury TAGS across ALL designations
CONF_STATE = HERE / "wnba_confirm_state.json"  # last-seen set of CONFIRMED-lineup teams (for diffing)

FIRM = ("OUT", "DOUBTFUL")                     # tags that drive the firm beneficiary scan
WATCH = ("QUESTIONABLE", "GTD")                # tags that drive the questionable-tier watchlist


def diff_report(prev_all, cur_all):
    """Compare two {player: TAG} injury snapshots and return the FULL change breakdown for every
    designation. Pure (no I/O) so it's unit-testable:
      added   {player: tag}         — newly on the report under any tag
      removed {player: old_tag}     — dropped off the report entirely (now active/cleared)
      changed {player: (old, new)}  — moved between tags (escalation or downgrade)
    plus the derived action sets:
      new     {player: tag}  — newly reads OUT/DOUBTFUL (fresh, or escalated up from a Q/GTD) -> firm scan
      new_q   {player: tag}  — newly reads QUESTIONABLE/GTD (and not also a firm add)        -> watchlist scan
      back    [player]       — was OUT/DOUBTFUL, now gone      -> void beneficiary plays
      back_q  [player]       — was QUESTIONABLE/GTD, now gone  -> void the watchlist spot"""
    added = {n: t for n, t in cur_all.items() if n not in prev_all}
    removed = {n: prev_all[n] for n in prev_all if n not in cur_all}
    changed = {n: (prev_all[n], cur_all[n]) for n in cur_all
               if n in prev_all and prev_all[n] != cur_all[n]}
    new = {n: t for n, t in added.items() if t in FIRM}
    new.update({n: t for n, (o, t) in changed.items() if t in FIRM and o not in FIRM})
    new_q = {n: t for n, t in added.items() if t in WATCH and n not in new}
    new_q.update({n: t for n, (o, t) in changed.items()
                  if t in WATCH and o not in WATCH and n not in new})
    back = sorted(n for n, t in removed.items() if t in FIRM)
    back_q = sorted(n for n, t in removed.items() if t in WATCH)
    return {"added": added, "removed": removed, "changed": changed,
            "new": new, "new_q": new_q, "back": back, "back_q": back_q}
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


def key_status(playing, inj, board, pl):
    """{player: TAG} for EVERY key (>=20 mpg OR >=10 ppg) player on the slate carrying ANY injury
    tag — OUT / DOUBTFUL / QUESTIONABLE from ESPN, plus GTD from RotoWire when ESPN hasn't tagged
    them. The complete report snapshot the loop diffs each poll. OUT/DOUBTFUL are filtered by
    `not playing_now` (a returning player still tagged out but with a full posted prop slate is
    really playing); QUESTIONABLE/GTD are NOT (books post props for questionable players)."""
    st = {}
    for n, s in inj.items():
        p = pl.get(n)
        if not (p and p.get("team") in playing and (p["min"] >= KEY_MIN or p["pts"] >= 10)):
            continue
        if s in ("Out", "Doubtful") and not T.playing_now(n):
            st[n] = s.upper()
        elif s == "Questionable":
            st[n] = "QUESTIONABLE"
    norm2name = {RW.norm(n): n for n in pl}
    for nnm in RW.questionable_players(board):
        full = norm2name.get(nnm)
        if (full and full not in st and pl.get(full, {}).get("team") in playing
                and (pl[full]["min"] >= KEY_MIN or pl[full]["pts"] >= 10)):
            st[full] = "GTD"
    return st


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
    # UNIFIED injury-report snapshot across ALL tags (OUT/DOUBTFUL/QUESTIONABLE/GTD), for a
    # comprehensive who's-new / who's-off / who-changed diff every poll.
    cur_all = key_status(playing, inj, board, pl)
    first_run = not STATUS_STATE.exists()
    prev_all = json.loads(STATUS_STATE.read_text()) if STATUS_STATE.exists() else {}
    STATUS_STATE.write_text(json.dumps(cur_all, indent=1, sort_keys=True))  # deterministic -> stable
    # CALIBRATION LOG: record every key questionable/doubtful/GTD player on tonight's slate (once, via
    # INSERT-OR-IGNORE) so wnba_question_log can resolve sit-vs-play later and recalibrate SIT_PROB.
    try:
        import wnba_question_log as QL
        today_et = dt.datetime.now(T.ET).date().isoformat()
        tips = T.tip_times()                                      # team -> tip (for lead time)
        norm2name = {RW.norm(nn): nn for nn in pl}
        obs, seen = [], set()

        def _tip(team):
            t = tips.get(team)
            return t.isoformat() if t is not None else None

        for nn, s in inj.items():                                 # ESPN Questionable / Doubtful
            if s in ("Questionable", "Doubtful") and nn in pl and pl[nn]["team"] in playing \
                    and (pl[nn]["min"] >= 20 or pl[nn]["pts"] >= 10):
                obs.append((nn, pl[nn]["team"], s, pl[nn]["min"], _tip(pl[nn]["team"])))
                seen.add(nn)
        for nnm in RW.questionable_players(board):                # RotoWire GTD
            full = norm2name.get(nnm)
            if full and full not in seen and pl[full]["team"] in playing \
                    and (pl[full]["min"] >= 20 or pl[full]["pts"] >= 10):
                obs.append((full, pl[full]["team"], "GTD", pl[full]["min"], _tip(pl[full]["team"])))
        if obs:
            QL.record(today_et, obs)
    except Exception as e:
        print("question-log record skipped:", str(e)[:60])
    if first_run:
        # cold start: baseline the whole report, don't fire it all as "news". The 4x/day full board
        # covers already-known tags; from here on this watcher pushes only genuine deltas.
        print(f"cold start — baselined {len(cur_all)} tagged key players, no push")
        return

    # ---- UNIFIED INJURY-REPORT DIFF across ALL tags (the comprehensive who's-new/off/changed feed) ----
    d = diff_report(prev_all, cur_all)
    added, removed, changed = d["added"], d["removed"], d["changed"]
    new, new_q, back, back_q = d["new"], d["new_q"], d["back"], d["back_q"]
    topic = os.environ.get("NTFY_TOPIC")
    if not (added or removed or changed):
        if conf_changed:
            # lineups locked in (no report change) — re-run the scan so the ledger's confidence
            # labels flip likely->confirmed/bench and the dashboard regenerates. No push.
            print(f"lineups confirmed ({len(conf_sig)} teams) — refreshing confidence, no push")
            _, preds = A.collect()
            L.log_predictions(preds)
        else:
            print(f"no report changes ({len(cur_all)} tagged: {', '.join(sorted(cur_all)) or 'none'})")
        return

    def _tag(t):
        return {"QUESTIONABLE": "Questionable", "GTD": "GTD", "OUT": "OUT", "DOUBTFUL": "Doubtful"}.get(t, t)
    feed = ([f"➕ {A._short(n)} → {_tag(added[n])}" for n in sorted(added)]
            + [f"↕ {A._short(n)}: {_tag(changed[n][0])} → {_tag(changed[n][1])}" for n in sorted(changed)]
            + [f"➖ {A._short(n)} OFF report (was {_tag(removed[n])}) — now active/cleared"
               for n in sorted(removed)])
    feed_txt = "\n".join(feed)
    print("REPORT CHANGES:\n" + feed_txt)

    # FAST REPLACEMENT READ for newly-firm outs: who inherits the vacated shots/role, before the
    # line moves. Only for `new` (fresh OUT/DOUBTFUL); a questionable/removal-only change no-ops it.
    repl = []
    if new:
        try:
            pl_all = W.players()
            by_team = {}
            for on in new:
                v = pl_all.get(on)
                if v and v.get("team"):
                    by_team.setdefault(v["team"], []).append(on)
            for team, outs in by_team.items():
                lu = DP.projected_lineup(team, outs, pl_all)
                for u in lu["usage_up"][:3]:
                    repl.append(f"↳ {team}: {A._short(u['name'])} absorbs +{u['d_fga']:g} FGA "
                                f"(→{u['fga_wo']:g}/g w/o {A._short(u['vs'])})")
                for p in lu["promoted"]:
                    cands = " / ".join(A._short(c) for c in p.get("candidates", [p["name"]]))
                    repl.append(f"↳ {team}: {A._short(p['replaces'])}'s spot → likely {cands} "
                                f"(shortlist; exact starter ~coin-flip)")
        except Exception as e:
            print("lineup read skipped:", str(e)[:60])
        for r in repl:
            print("  " + r)

    # run the beneficiary scan only when a PLAY could change (new firm out OR new questionable);
    # a pure removal/downgrade still pushes the change feed but needs no re-scan.
    fresh = []
    if new or new_q:
        alerts, preds = A.collect()
        logged = L.log_predictions(preds)
        seen = set(A.SEEN.read_text().splitlines()) if A.SEEN.exists() else set()
        this_run = set()
        for ev, k, msg in alerts:                       # alerts sorted by EV desc
            if k in seen or k in this_run:
                continue
            this_run.add(k)
            fresh.append((ev, k, msg))
        print(f"wnba-watch: {len(alerts)} spots, {len(fresh)} new, {logged} logged to ledger")
        for _e, _k, m in fresh:
            print("  " + m)

    # ---- ONE comprehensive push: report changes -> replacement read -> plays -> void warnings ----
    if topic:
        parts = ["📋 WNBA injury update", feed_txt]
        if repl:
            parts.append("\n".join(repl))
        if fresh:
            parts.append(A._notif_body(fresh))
        if back or back_q:
            voids = ", ".join(A._short(n) for n in back + back_q)
            parts.append(f"⚠ VOID any plays built on: {voids} (now active/cleared)")
        body = "\n\n".join(parts)
        prio = "urgent" if new else ("high" if (new_q or back or back_q) else "default")
        tags = "rotating_light" if new else "hourglass_flowing_sand"
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers={"Title": "WNBA injury update", "Priority": prio, "Tags": tags},
                          timeout=15)
            print(f"pushed ({prio})")
        except requests.RequestException as e:
            print("push failed:", e)
    if fresh:                                           # remember pushed spots so they don't re-fire
        seen = set(A.SEEN.read_text().splitlines()) if A.SEEN.exists() else set()
        for _e, k, _m in fresh:
            seen.add(k)
        A.SEEN.write_text("\n".join(sorted(seen)[-2000:]))


if __name__ == "__main__":
    main()
