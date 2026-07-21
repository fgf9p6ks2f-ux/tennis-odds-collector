"""Model-vs-LINE backtest for pitcher strikeouts — the make-or-break test.

Does the K model beat an actual posted line after vig? We grade against the collected
PINNACLE K lines (the sharpest book — the hardest bar; beat Pinnacle and you crush FanDuel).

For each (pitcher, game-date) with a Pinnacle strikeout line:
  1. project mean Ks WALK-FORWARD (only that pitcher's starts STRICTLY BEFORE that date
     + opponent team K%, log5-combined) — no leak,
  2. P(over) via NB with the RECALIBRATED dispersion (fit below, not the stale size=8),
  3. devig Pinnacle -> fair P; bet the +EV side at Pinnacle's decimal price,
  4. grade vs the pitcher's ACTUAL Ks that day; book PnL.

Dispersion is refit first from every 2026 walk-forward start (moment match), so the
alt/line probabilities use the true spread. Caches name->id + gamelogs to scratchpad.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import nbinom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mlb import data  # noqa: E402

SEASON = 2026
LG_FALLBACK = 0.22
PRIOR_BF = 120.0
CACHE = Path("/private/tmp/claude-501/-Users-ethandown-Desktop-Projects/"
             "99923ba4-f2c4-4498-a394-ec835e95e0a5/scratchpad/mlb_kbt_cache.json")


def load_cache():
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except ValueError:
            pass
    return {"ids": {}, "logs": {}}


def save_cache(c):
    CACHE.write_text(json.dumps(c))


def closing_lines():
    """Last snapshot before start per (pitcher, game-date): (name, date, line, over_dec, under_dec)."""
    con = sqlite3.connect("mlb_kprops.sqlite")
    rows = con.execute("""
        SELECT pitcher, date(start_time) gd, line, over_odds, under_odds, collected_at
        FROM pitcher_props
        WHERE stat='strikeouts' AND start_time IS NOT NULL
          AND collected_at <= start_time
        ORDER BY pitcher, gd, collected_at""").fetchall()
    con.close()
    last = {}
    for name, gd, line, oo, uo, cat in rows:
        if oo and uo:
            last[(name, gd)] = (line, oo, uo)          # later row overwrites -> closing
    return [(n, gd, *v) for (n, gd), v in last.items()]


def proj_mean(prior_starts, opp_kpct, lg_k):
    """Walk-forward projected mean Ks from a pitcher's PRIOR starts + opponent K% (log5)."""
    k_sum = sum(s["k"] for s in prior_starts)
    bf_sum = sum(s["bf"] for s in prior_starts)
    n = len(prior_starts)
    k_pct = (k_sum + PRIOR_BF * LG_FALLBACK) / (bf_sum + PRIOR_BF)
    exp_bf = float(np.clip(bf_sum / max(n, 1), 18, 27))
    a = k_pct * opp_kpct / lg_k
    b = (1 - k_pct) * (1 - opp_kpct) / (1 - lg_k)
    rate = a / (a + b)
    return rate * exp_bf


def p_over(mean, line, size):
    p = size / (size + mean)
    return float(1 - nbinom.cdf(np.floor(line), size, p))


def main():
    cache = load_cache()
    tk, lg_k = data.team_kpct(SEASON)
    lines = closing_lines()
    print(f"loaded {len(lines)} closing Pinnacle K lines; resolving pitchers + gamelogs...")

    # resolve ids + fetch gamelogs (cached)
    logs = {}          # name -> gamelog (list of starts, chronological)
    unresolved = 0
    for name, gd, line, oo, uo in lines:
        if name in logs:
            continue
        pid = cache["ids"].get(name)
        if pid is None and name not in cache["ids"]:
            pid = data.find_pitcher(name)
            cache["ids"][name] = pid
        if not pid:
            unresolved += 1
            continue
        key = str(pid)
        if key not in cache["logs"]:
            try:
                cache["logs"][key] = data.pitcher_gamelog(pid, SEASON)
            except Exception:
                cache["logs"][key] = []
        logs[name] = cache["logs"][key]
    save_cache(cache)

    # ---- Step 1: recalibrate dispersion from ALL 2026 walk-forward starts (moment match) ----
    dproj, dact = [], []
    for name, gl in logs.items():
        gl = sorted(gl, key=lambda g: g["date"] or "")
        for i, g in enumerate(gl):
            if i < 3 or g["bf"] < 5:
                continue
            pm = proj_mean(gl[:i], tk.get(g["opp_id"], lg_k), lg_k)
            dproj.append(pm); dact.append(g["k"])
    dproj, dact = np.array(dproj), np.array(dact)
    resid2 = float(np.mean((dact - dproj) ** 2))
    mp, mp2 = float(dproj.mean()), float(np.mean(dproj ** 2))
    size = mp2 / (resid2 - mp) if resid2 > mp else 500.0
    vm = float(np.var(dact) / np.mean(dact))
    print(f"\n=== dispersion recalibration ({len(dproj)} walk-forward 2026 starts) ===")
    print(f"  proj {mp:.2f} vs actual {dact.mean():.2f} (bias {dact.mean()-mp:+.2f})  "
          f"corr {np.corrcoef(dproj,dact)[0,1]:.3f}  var/mean {vm:.2f}")
    print(f"  moment-fit NB size = {size:.1f}   (old code default 8; higher = closer to Poisson)")

    # ---- Step 3: model vs Pinnacle line, bet the +EV side at Pinnacle price ----
    bets = []      # (edge, side, dec_odds, won, model_p, fair_p, line, proj, actual, name, gd)
    graded = skipped = 0
    for name, gd, line, oo, uo in lines:
        gl = logs.get(name)
        if not gl:
            skipped += 1; continue
        cur = next((g for g in gl if g["date"] == gd), None)
        if cur is None or cur["bf"] < 5:
            skipped += 1; continue                     # scratched / didn't actually start
        prior = [g for g in gl if (g["date"] or "") < gd and g["bf"] >= 5]
        if len(prior) < 3:
            skipped += 1; continue                     # too few priors to project
        pm = proj_mean(prior, tk.get(cur["opp_id"], lg_k), lg_k)
        mp_over = p_over(pm, line, size)
        # devig Pinnacle
        io, iu = 1/oo, 1/uo
        fair_over = io / (io + iu)
        actual = cur["k"]
        # EV per side at the offered price
        ev_over = mp_over * (oo - 1) - (1 - mp_over)
        ev_under = (1-mp_over) * (uo - 1) - mp_over
        if ev_over >= ev_under and ev_over > 0:
            side, dec, won, edge = "over", oo, actual > line, mp_over - fair_over
        elif ev_under > 0:
            side, dec, won, edge = "under", uo, actual < line, (1-mp_over) - (1-fair_over)
        else:
            graded += 1; continue                      # no +EV side
        bets.append((edge, side, dec, won, mp_over, fair_over, line, pm, actual, name, gd))
        graded += 1

    print(f"\ngraded {graded} lines ({skipped} skipped: unresolved/scratched/too-few-priors)")

    def roi(sel):
        if not sel:
            return (0, 0, 0, 0.0, 0.0)
        w = sum(1 for b in sel if b[3])
        pnl = sum((b[2]-1) if b[3] else -1 for b in sel)
        return (len(sel), w, len(sel)-w, pnl, pnl/len(sel)*100)

    print("\n=== ROI at Pinnacle price by min-edge threshold (the hard bar) ===")
    print(f"{'edge>=':>7}{'n':>5}{'W':>5}{'L':>5}{'units':>9}{'ROI%':>8}")
    for thr in (0.0, 0.02, 0.04, 0.06, 0.08):
        sel = [b for b in bets if b[0] >= thr]
        n, w, l, pnl, r = roi(sel)
        print(f"{thr*100:>6.0f}%{n:>5}{w:>5}{l:>5}{pnl:>9.2f}{r:>8.1f}")

    print("\n=== by side (edge>=2%) ===")
    for s in ("over", "under"):
        n, w, l, pnl, r = roi([b for b in bets if b[1] == s and b[0] >= 0.02])
        print(f"  {s:5} n={n:>4}  {w}-{l}  units={pnl:+.2f}  ROI={r:+.1f}%")

    # signal test: do higher-edge bets actually hit more? (independent of vig level)
    print("\n=== signal test: realized over-rate by model edge decile (calibration) ===")
    allb = sorted(bets, key=lambda b: b[0])
    if allb:
        import numpy as _np
        for q0, q1, lbl in [(0.0,0.5,"low edge"), (0.5,1.0,"high edge")]:
            seg = allb[int(len(allb)*q0):int(len(allb)*q1)]
            if seg:
                hit = sum(1 for b in seg if b[3]) / len(seg)
                avg_edge = _np.mean([b[0] for b in seg])
                print(f"  {lbl:10} n={len(seg):>4}  avg model edge {avg_edge*100:+.1f}%  realized win {hit*100:.1f}%")

    # save the graded bets for the loss analysis (step 2 prep)
    out = [{"name": b[9], "date": b[10], "side": b[1], "line": b[6], "dec": b[2],
            "won": b[3], "model_p_over": b[4], "fair_p_over": b[5], "proj": b[7],
            "actual": b[8], "edge": b[0]} for b in bets]
    Path(CACHE.parent / "k_bets.json").write_text(json.dumps(out, indent=1))
    print(f"\nsaved {len(out)} graded bets -> k_bets.json (for loss analysis)")


if __name__ == "__main__":
    main()
