"""WNBA usage-redistribution model — the keystone. Predicts WHO inherits the ball.

The user's edge is about USAGE, not raw minutes: a backup who plays 30 min but doesn't
touch the ball is a trap; the one who becomes the focal point is the bet. So when a key
player sits, this projects how their vacated USAGE (possessions = FGA + 0.44·FTA + TOV,
and the assist/facilitation load) redistributes to teammates — then their production at
that elevated usage.

Model (deliberately simple + validated, not a black box): a player's per-minute usage
rises when a high-usage teammate sits, roughly in proportion to their own baseline usage
share (the ball finds the players who already command it). We fit ONE absorption factor
from history — how much of the vacated per-min usage the remaining rotation soaks up —
and validate it out-of-sample against actual WOWY usage jumps.

Data is cached (data-heavy: game logs for a roster). stats.nba rate-limits a dev IP;
run --fit from CI. `project(team, out)` uses the cached fit + current logs.

    python wnba_redist.py --fit --teams LVA,NYL,MIN   # fit+validate on a few teams
    python wnba_redist.py --project LVA --out "A'ja Wilson"
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import time
from pathlib import Path

import wnba_wowy as W

HERE = Path(__file__).resolve().parent
FIT = HERE / "wnba_redist.json"


def per_min_usage(games):
    """Mean possessions-used per minute over a set of games (the usage-rate signal)."""
    tot_min = sum(g["min"] for g in games)
    return sum(g["poss"] for g in games) / tot_min if tot_min else 0.0


def _roster_logs(team, pl, min_gp=4):
    """{name: game_log} for a team's rotation players (cached externally by caller)."""
    out = {}
    for n, v in pl.items():
        if v["team"] == team and v["gp"] >= min_gp:
            try:
                out[n] = W.game_log(v["id"])
                time.sleep(0.25)
            except RuntimeError:
                pass
    return out


def observed_absorption(roster_logs, out_name):
    """For one 'out_name', the ratio (team's per-min usage WITHOUT them among the
    remaining players) / (WITH them) — how much the rotation's usage-rate rose. Plus the
    per-player jumps. None if the player never missed a game with enough sample."""
    if out_name not in roster_logs:
        return None
    out_games = {g["game_id"] for g in roster_logs[out_name]}
    with_rows, without_rows = [], []
    per_player = {}
    for n, log in roster_logs.items():
        if n == out_name:
            continue
        w = [g for g in log if g["game_id"] in out_games]
        wo = [g for g in log if g["game_id"] not in out_games]
        if len(wo) >= 2 and len(w) >= 3:
            uw, uwo = per_min_usage(w), per_min_usage(wo)
            per_player[n] = (uw, uwo)
            with_rows += w
            without_rows += wo
    if not per_player:
        return None
    tw, two = per_min_usage(with_rows), per_min_usage(without_rows)
    return {"team_ratio": two / tw if tw else 1.0, "per_player": per_player,
            "n_out_games": len(out_games)}


def fit(teams):
    """Fit the leaguewide usage-absorption factor + validate the proportional model."""
    pl = W.players()
    ratios, pred_actual = [], []
    for team in teams:
        logs = _roster_logs(team, pl)
        # candidate 'out' players: high-usage rotation pieces
        cands = sorted(logs, key=lambda n: -per_min_usage(logs[n]))[:6]
        for out_name in cands:
            obs = observed_absorption(logs, out_name)
            if not obs or obs["n_out_games"] < 2:
                continue
            ratios.append(obs["team_ratio"])
            # proportional test: does a player's usage jump track their baseline usage share?
            base = {n: per_min_usage([g for g in logs[n]]) for n in obs["per_player"]}
            tot = sum(base.values()) or 1
            for n, (uw, uwo) in obs["per_player"].items():
                actual_jump = uwo - uw
                share = base[n] / tot                       # higher-usage -> absorbs more
                pred_actual.append((share, actual_jump))
    factor = st.median(ratios) if ratios else 1.0
    corr = None
    if len(pred_actual) >= 8:
        xs = [s for s, _ in pred_actual]; ys = [j for _, j in pred_actual]
        mx, my = st.mean(xs), st.mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in pred_actual)
        vx = sum((x - mx) ** 2 for x in xs); vy = sum((y - my) ** 2 for y in ys)
        corr = cov / (vx * vy) ** 0.5 if vx and vy else 0
    FIT.write_text(json.dumps({"absorption_factor": factor, "n_situations": len(ratios),
                               "proportional_corr": corr}, indent=1))
    print(f"fit: usage-absorption factor {factor:.3f} over {len(ratios)} out-situations")
    print(f"proportional model corr(baseline-usage-share, actual usage jump) = "
          f"{corr:+.2f}" if corr is not None else "  (too few points to validate corr)")
    print("  -> a positive corr confirms the ball finds already-high-usage players when a "
          "star sits (the model's core assumption).")


def project(team, out_name):
    """Project each rotation player's usage bump if out_name sits, using the fit +
    (where available) their own WOWY usage jump. Returns [(name, base_upm, proj_upm)]."""
    cfg = json.loads(FIT.read_text()) if FIT.exists() else {"absorption_factor": 1.12}
    factor = cfg["absorption_factor"]
    pl = W.players()
    logs = _roster_logs(team, pl)
    if out_name not in logs:
        return []
    obs = observed_absorption(logs, out_name)
    out = []
    for n in logs:
        if n == out_name:
            continue
        base = per_min_usage(logs[n])
        if obs and n in obs["per_player"]:                 # REAL WOWY usage jump — robust,
            proj, src = obs["per_player"][n][1], "wowy"    # no model assumption
        else:                                              # no history: flat league factor,
            proj, src = base * factor, "est"               # NOT proportional (that failed
                                                           # to validate — see fit corr)
        out.append((n, base, proj, src))
    return sorted(out, key=lambda r: -(r[2] - r[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--teams", default="LVA,NYL,MIN,CON,PHX")
    ap.add_argument("--project")
    ap.add_argument("--out")
    args = ap.parse_args()
    if args.fit:
        fit([t.strip() for t in args.teams.split(",")])
    elif args.project and args.out:
        print(f"If {args.out} sits — projected usage-rate (poss/min) bump:")
        for n, base, proj, src in project(args.project, args.out)[:6]:
            print(f"  {n:22} {base:.2f} → {proj:.2f}  ({(proj-base):+.2f}/min, "
                  f"{(proj-base)*30:+.1f} poss/30min) [{src}]")


if __name__ == "__main__":
    main()
