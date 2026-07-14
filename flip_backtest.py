"""Backtest the role_flip on graded unders that have a backfilled OVER price (odds_other).

For each graded UNDER we bet, replay the production guard+flip decision LEAK-FREE — WOWY
computed only from games STRICTLY BEFORE the bet date, i.e. exactly what the live scorer saw
at flag time (using post-bet games would leak the outcome into the decision):
  GATE 1 (guard veto): n_without >= ROLE_GUARD_MINN AND without-star mean >= line
  GATE 2 (flip emit) : re-score the OVER on the without-star games; emit if eo >= OVER_EV_MIN
The flipped OVER is then graded at the real backfilled price: it WINS iff actual > line
(result=='over'). Constants + math are imported from wnba_tonight so this tracks production.

Run: python3 flip_backtest.py   (rerun as more flips accumulate forward — today it fires 0x)
"""
import sqlite3
from pathlib import Path

import wnba_wowy as W
from wnba_tonight import PROP_STATS, ROLE_GUARD_MINN, OVER_EV_MIN, ROLE_FLOOR, flip_p_over

HERE = Path(__file__).resolve().parent
led = sqlite3.connect(HERE / "wnba_ledger.sqlite")
led.row_factory = sqlite3.Row
unders = [dict(r) for r in led.execute(
    "SELECT pred_date, player, out_player, stat, line, odds, odds_other, result, proj_min "
    "FROM predictions WHERE side='under' AND result IN ('over','under') AND odds_other IS NOT NULL")]

pl = W.players()
cache = {}
def glog(name):
    p = pl.get(name)
    if not p:
        return None
    if name not in cache:
        try:
            cache[name] = W.game_log(p["id"])
        except Exception:
            cache[name] = []
    return cache[name]

def before(log, date):
    return [g for g in log if (g.get("date", "")[:10] < date)]   # leak-free: pre-bet games only

flips, vetoed, not_vetoed, no_data = [], [], 0, 0
for r in unders:
    b = glog(r["player"])
    outs = [o for o in (glog(x.strip()) for x in (r["out_player"] or "").split(",") if x.strip()) if o]
    if not b or not outs:
        no_data += 1
        continue
    w = W.wowy_multi(before(b, r["pred_date"]), [before(o, r["pred_date"]) for o in outs])
    key = PROP_STATS.get(r["stat"], r["stat"])
    wblk = w["without"].get(key) or {}
    wo, wvals = wblk.get("mean"), wblk.get("vals")
    line, over_dec = r["line"], r["odds_other"]
    if not (w["n_without"] >= ROLE_GUARD_MINN and wo is not None and wo >= line):
        not_vetoed += 1                              # guard would NOT veto -> stays a normal under
        continue
    floor = max((r["proj_min"] or 0) - 4, ROLE_FLOOR)
    shrink_k = 11 if len([g for g in before(b, r["pred_date"]) if (g.get("min") or 0) >= floor]) >= 4 else 14
    nw = len(wvals) if wvals else 0
    p_over = eo = None
    if wvals and nw >= ROLE_GUARD_MINN and 1.6 <= over_dec <= 5.0:
        p_over = flip_p_over(wvals, line)                    # shared with prop_edges (no drift)
        po = (p_over * nw + (1.0 / over_dec) * shrink_k) / (nw + shrink_k)
        eo = po * over_dec - 1
    rec = {**r, "over_dec": over_dec, "eo": eo, "ho": p_over, "wo": wo, "nw": nw, "win": r["result"] == "over"}
    (flips if (eo is not None and eo >= OVER_EV_MIN) else vetoed).append(rec)

def summarize(lst):
    n = len(lst); wins = sum(x["win"] for x in lst)
    roi = sum((x["over_dec"] - 1) if x["win"] else -1 for x in lst) / n if n else 0
    return wins, n - wins, (100 * wins / n if n else 0), roi

print(f"graded unders with a backfilled over price: {len(unders)}  (no WOWY data: {no_data})")
print(f"  guard-vetoed -> FLIPPED to over (emitted):   {len(flips)}")
print(f"  guard-vetoed -> flip dropped (over not +EV):  {len(vetoed)}")
print(f"  not vetoed (normal unders, guard passes):     {not_vetoed}")
w, l, hit, roi = summarize(flips)
print(f"\n=== FLIP-OVER record: {w}-{l} ({hit:.0f}% hit)   ROI {roi:+.1%}/unit   (n={len(flips)}) ===")
for x in sorted(flips, key=lambda z: z["pred_date"]):
    print(f"    {x['pred_date']} {x['player'][:20]:20} {x['stat']:8} o{x['line']:<5g} @{x['over_dec']:.2f}  "
          f"eo{x['eo']:+.2f} P(ovr){x['ho']:.0%} wo{x['wo']:.1f} n{x['nw']} -> {'WIN' if x['win'] else 'loss'}")

print("\n--- guard-vetoed unders where the flip did NOT fire (why + what the under did) ---")
uv_win = sum(1 for x in vetoed if x["result"] == "under")
print(f"    as UNDERs these went {uv_win}-{len(vetoed) - uv_win} (guard-SUPPRESS value: losses avoided vs wins sacrificed)")
for x in sorted(vetoed, key=lambda z: z["pred_date"]):
    eo_s = f"{x['eo']:+.2f}" if x["eo"] is not None else "n/a"
    hr = f"{x['ho']:.0%}" if x["ho"] is not None else "n/a"
    print(f"    {x['pred_date']} {x['player'][:20]:20} {x['stat']:8} line{x['line']:<5g} over@{x['over_dec']:.2f} "
          f"wo{x['wo']:.1f}(n{x['nw']}) P(ovr){hr} -> flip eo {eo_s} | under {'WON' if x['result'] == 'under' else 'LOST'}")
