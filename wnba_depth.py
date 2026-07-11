"""Depth-chart REPLACEMENT projector — the front of the timing edge.

The instant a starter is ruled out, name who slides into the role off the bench and project
their minutes, BEFORE the box score (or even RotoWire) confirms it. The signal is the player's
minutes-WOWY vs the out player (who actually plays more when X sits), weighted by position match
(a guard replaces a guard) and a bench->starter jump, and hard-confirmed by RotoWire when the
lineup is posted. Whoever tops that ranking is the beneficiary to price before the book moves.

    python wnba_depth.py --team LV --out "A'ja Wilson"   # who replaces her + projected minutes
    python wnba_depth.py --validate                       # did our pick actually get the minutes?
"""
import argparse
import statistics as st

import rotowire as RW
import wnba_wowy as W

PG = {"G": "G", "PG": "G", "SG": "G", "GF": "G", "F": "F", "SF": "F", "PF": "F",
      "FC": "C", "C": "C", "CF": "C"}
PRIMARY_FGA = 13.0    # baseline FGA at/above this = a primary option who shoots regardless of who's
#                       out (Mabrey) — NOT a volume beneficiary. Matches wnba_tonight.PRIMARY_FGA.
_COMPAT = {("G", "F"): 0.3, ("F", "G"): 0.3, ("F", "C"): 0.6, ("C", "F"): 0.6,
           ("G", "C"): 0.15, ("C", "G"): 0.15}


def _pos(p):
    return PG.get(str(p or "").upper(), "F")


def position_compat(a, b):
    a, b = _pos(a), _pos(b)
    return 1.0 if a == b else _COMPAT.get((a, b), 0.3)


def team_rotation(team, players=None, min_games=4):
    """[(pid, name, position, game_log)] for a team's rotation (>=min_games played)."""
    players = players or W.players()
    out = []
    for name, v in players.items():
        if str(v.get("team", "")).upper() != team.upper():
            continue
        try:
            log = [g for g in W.game_log(v["id"]) if g["min"] > 0]
        except Exception:
            log = []
        if len(log) >= min_games:
            out.append((v["id"], name, v.get("position"), log))
    return out


def replacements(out_pid, out_pos, out_log, rotation, starters=None):
    """Rank the likely replacements for an out player + project each one's minutes in the new role.
    `starters` = RotoWire-confirmed starter names (optional hard signal)."""
    omin = st.mean(g["min"] for g in out_log if g["min"] > 0) if out_log else 0.0
    snorm = {RW.norm(s) for s in (starters or [])}
    cands = []
    for pid, name, pos, log in rotation:
        if pid == out_pid or len(log) < 3:
            continue
        w = W.wowy(log, out_log)
        wi, nwi = w["with"]["min"]["mean"], w["with"]["min"]["n"]
        wo, nwo = w["without"]["min"]["mean"], w["without"]["min"]["n"]
        pm = position_compat(pos, out_pos)
        confirmed = RW.norm(name) in snorm
        season_min = st.mean(g["min"] for g in log)
        # WOWY minutes-gain counts ONLY when both splits are real — a long-term-out player (tiny
        # 'with' sample) or a no-history injury makes the delta garbage, so fall back to position
        # depth (the same-position rotation player, weighted by minutes = the next man up).
        reliable = nwi >= 3 and nwo >= 2
        dmin = (wo - wi) if reliable else 0.0
        was_bench = wi < 22 and nwi >= 3
        proj_min = round(wo, 1) if nwo >= 2 else round(wi + pm * omin * 0.5, 1)
        depth = pm * season_min / 6.0                        # position-matched depth (next man up)
        score = (max(dmin, 0.0) * pm + depth
                 + (2.0 * pm if (was_bench and dmin > 3) else 0.0)
                 + (12.0 if confirmed else 0.0))
        if score <= 0 and not confirmed:
            continue
        cands.append({"name": name, "pos": _pos(pos), "proj_min": proj_min, "d_min": round(dmin, 1),
                      "n_without": nwo, "confirmed": confirmed, "was_bench": was_bench,
                      "score": round(score, 2)})
    cands.sort(key=lambda c: -c["score"])
    return cands


def primary(out_pid, out_pos, out_log, rotation, starters=None):
    """The single most likely replacement (or None)."""
    r = replacements(out_pid, out_pos, out_log, rotation, starters)
    return r[0] if r else None


def base_five(team, players=None, recent=8):
    """The team's usual starting five = the 5 highest recent-minute players (our own projected
    lineup, no RotoWire needed). ESPN's per-game starter flag is post-tip; minutes are the robust
    pre-news proxy for who starts."""
    scored = []
    for pid, name, pos, log in team_rotation(team, players):
        rec = sorted(log, key=lambda g: g["date"])[-recent:]
        scored.append((st.mean(g["min"] for g in rec), pid, name, pos, log))
    scored.sort(key=lambda x: -x[0])
    return [(pid, name, pos, log) for _m, pid, name, pos, log in scored[:5]]


def projected_lineup(team, out_names, players=None, confirmed=None):
    """OUR projected starting five given tonight's injury report — the RotoWire replacement, built
    from our own data so we can fire the instant news breaks. Take the usual 5, drop whoever's out,
    and promote the best position-matched bench replacement into each vacated slot. Returns the
    projected five, who's NEWLY promoted (the beneficiaries) + their minutes, and the vacated slots.
    `confirmed` (RotoWire) overrides when posted — we only ever need them for the confirmed lineup."""
    players = players or W.players()
    onorm = {RW.norm(o) for o in (out_names or [])}
    rot = team_rotation(team, players)
    base = base_five(team, players)
    base_ids = {pid for pid, *_ in base}
    kept = [(pid, name, pos, log) for pid, name, pos, log in base if RW.norm(name) not in onorm]
    vacated = [(pid, name, pos, log) for pid, name, pos, log in base if RW.norm(name) in onorm]
    bench = [(pid, name, pos, log) for pid, name, pos, log in rot
             if pid not in base_ids and RW.norm(name) not in onorm]
    promoted = []
    for opid, oname, opos, olog in vacated:
        cands = replacements(opid, opos, olog, bench, confirmed)
        if not cands:
            continue
        top = cands[0]
        bp = next((b for b in bench if RW.norm(b[1]) == RW.norm(top["name"])), None)
        if bp:
            promoted.append({"name": top["name"], "pos": top["pos"], "proj_min": top["proj_min"],
                             "replaces": oname, "d_min": top["d_min"], "confirmed": top["confirmed"]})
            bench = [b for b in bench if b[0] != bp[0]]
    # USAGE beneficiaries: existing starters who stay in the five but absorb the out player's SHOTS
    # (Hamby/Burrell when Plum sits) — distinct from who fills the empty slot. This is the volume
    # signal that drives the points bets, keyed on the highest-usage out player.
    usage = []
    if vacated:
        opid, oname, opos, olog = max(vacated, key=lambda v: st.mean(g["min"] for g in v[3]))
        for pid, name, pos, log in kept:
            w = W.wowy(log, olog)
            wi, nwi = w["with"]["fga"]["mean"], w["with"]["fga"]["n"]
            wo, nwo = w["without"]["fga"]["mean"], w["without"]["fga"]["n"]
            # ROOM TO GROW: skip primary options (high baseline FGA) — they shoot regardless, so
            # their volume doesn't really rise off an injury. Only surface players who absorb shots.
            if nwi >= 3 and nwo >= 2 and wo - wi > 0.5 and wi < PRIMARY_FGA:
                usage.append({"name": name, "d_fga": round(wo - wi, 1), "fga_wo": round(wo, 1),
                              "vs": oname})
        usage.sort(key=lambda x: -x["d_fga"])
    return {"team": team, "starters": [n for _p, n, _po, _l in kept] + [p["name"] for p in promoted],
            "promoted": promoted, "usage_up": usage, "vacated": [n for _p, n, _po, _l in vacated]}


# ---- validation: did our pre-game pick actually get the minutes? -------------------------------
def _validate():
    players = W.players()
    id2team = {v["id"]: str(v.get("team", "")) for v in players.values()}
    id2pos = {v["id"]: v.get("position") for v in players.values()}
    logs = {}
    for v in players.values():
        try:
            logs[v["id"]] = sorted((g for g in W.game_log(v["id"]) if g["min"] > 0),
                                   key=lambda g: g["date"])
        except Exception:
            logs[v["id"]] = []
    # played[team][date] = {pid}
    from collections import defaultdict
    played = defaultdict(lambda: defaultdict(set))
    for pid, gs in logs.items():
        for g in gs:
            played[id2team.get(pid, "")][g["date"][:10]].add(pid)
    hits, n, maes = 0, 0, []
    for pid, gs in logs.items():
        if len(gs) < 8 or st.mean(g["min"] for g in gs) < 24:   # a rotation STARTER who might sit
            continue
        team = id2team.get(pid, "")
        dates = sorted(played[team])
        for i, g in enumerate(gs):
            d = g["date"][:10]
            # an OUT game = a later team game date where this starter did NOT play
            for dd in dates:
                if dd <= d or dd in {x["date"][:10] for x in gs}:
                    continue
                # build rotation from games strictly before dd
                rot = []
                for opid, ogs in logs.items():
                    prior = [x for x in ogs if x["date"][:10] < dd]
                    if id2team.get(opid) == team and len(prior) >= 4:
                        rot.append((opid, players_name(players, opid), id2pos.get(opid), prior))
                out_prior = [x for x in gs if x["date"][:10] < dd]
                if len(out_prior) < 4:
                    break
                pick = primary(pid, id2pos.get(pid), out_prior, rot)
                if not pick:
                    break
                # actual: on dd, who among the team gained the most minutes vs their season mean?
                on = played[team].get(dd, set())
                top = _top_gainers(team, dd, on, logs, k=3)
                if top:
                    n += 1
                    names = {RW.norm(t[0]) for t in top}
                    if RW.norm(pick["name"]) in names:
                        hits += 1
                        act_min = next(t[1] for t in top if RW.norm(t[0]) == RW.norm(pick["name"]))
                        maes.append(abs(pick["proj_min"] - act_min))
                break   # one out-game per starter is enough for a quick read
    print(f"our top pick is a TOP-3 minutes gainer: {hits}/{n} ({100*hits/n:.0f}%)" if n else "no samples")
    if maes:
        print(f"projected-minutes MAE when we're right: {st.mean(maes):.1f} min (n{len(maes)})")


def players_name(players, pid):
    for name, v in players.items():
        if v["id"] == pid:
            return name
    return str(pid)


def _top_gainers(team, date, on_pids, logs, k=3):
    gains = []
    for pid in on_pids:
        gs = logs.get(pid, [])
        today = next((g for g in gs if g["date"][:10] == date), None)
        prior = [g for g in gs if g["date"][:10] < date]
        if not today or len(prior) < 4:
            continue
        gain = today["min"] - st.mean(g["min"] for g in prior)
        if gain > 3:
            gains.append((players_name_by_id(pid), today["min"], gain))
    gains.sort(key=lambda x: -x[2])
    return gains[:k]


def players_name_by_id(pid):
    for name, v in W.players().items():
        if v["id"] == pid:
            return name
    return str(pid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team")
    ap.add_argument("--out")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--lineup", action="store_true")
    args = ap.parse_args()
    if args.validate:
        _validate()
        return
    if args.lineup:
        outs = [o.strip() for o in (args.out or "").split(",") if o.strip()]
        lu = projected_lineup(args.team, outs)
        print(f"\nprojected {args.team} starting five with {', '.join(outs) or 'nobody'} out:")
        print("  ", " · ".join(lu["starters"]))
        for p in lu["promoted"]:
            print(f"  STARTS: {p['name']} ~{p['proj_min']:g}min (fills {p['replaces']}'s slot)")
        for u in lu["usage_up"]:
            print(f"  usage↑: {u['name']} +{u['d_fga']:g} FGA/g (→{u['fga_wo']:g} w/o {u['vs']})")
        return
    players = W.players()
    opid = next((v["id"] for name, v in players.items() if RW.norm(name) == RW.norm(args.out)), None)
    opos = next((v.get("position") for name, v in players.items() if RW.norm(name) == RW.norm(args.out)), None)
    rot = team_rotation(args.team, players)
    olog = [g for g in W.game_log(opid) if g["min"] > 0] if opid else []
    try:
        starters = [nm for t in RW.board() if t["team"] == args.team.upper() for _, nm, _ in t["starters"]]
    except Exception:
        starters = []
    print(f"\n{args.out} OUT ({args.team}) — likely replacements:\n")
    for c in replacements(opid, opos, olog, rot, starters)[:5]:
        tag = " ✓CONFIRMED starting" if c["confirmed"] else (" (bench→starter)" if c["was_bench"] else "")
        print(f"  {c['name'][:22]:22} {c['pos']}  ~{c['proj_min']:g} min  (+{c['d_min']:g} w/o them, "
              f"n{c['n_without']}){tag}")


if __name__ == "__main__":
    main()
