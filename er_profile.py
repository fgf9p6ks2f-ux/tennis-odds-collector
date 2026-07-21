"""Profile the earned-runs UNDER bets: what statistically separates the WINS from the LOSSES?
Reuses er_backtest's matching. UNDER wins = actual ER < line; loss = actual ER > line. We compare
the two groups on opponent offense (OBP), contact (K%), home/away, the pitcher's recent-5 median ER,
and the line — to see if a filter (like away+contact was for outs) tightens the edge."""
import statistics as st
from mlb import data as D
from er_backtest import load_props, match_actual


def build():
    season = 2026
    tk, lg = D.team_kpct(season)
    tobp, lgobp = D.team_obp(season)
    close = load_props("earned_runs")
    gl, idc = {}, {}
    recs = []
    for (pit, gd), (line, oo, uo, stt) in close.items():
        g = match_actual(pit, gd, gl, idc)
        if not g or g.get("er") is None:
            continue
        recs.append({"line": float(line), "er": g["er"], "home": bool(g.get("is_home")),
                     "opp": g.get("opp_id"), "pid": idc.get(pit),
                     "obp": tobp.get(g.get("opp_id")), "k": tk.get(g.get("opp_id"))})
    for r in recs:
        prv = [x for x in gl.get(r["pid"], []) if x.get("date") and x.get("er") is not None]
        prv = sorted(prv, key=lambda x: x["date"])
        # recent-5 strictly before is hard here (no per-rec date), so use season-to-date median as a
        # cheap pitcher-quality proxy for the profile (direction only, not a live signal)
        r["q"] = st.median(x["er"] for x in prv) if len(prv) >= 3 else None
    return recs, lgobp, lg


def _m(vals):
    vals = [v for v in vals if v is not None]
    return st.mean(vals) if vals else float("nan")


def prof(name, rows):
    n = len(rows)
    if not n:
        print(f"  {name}: (none)"); return
    print(f"  {name:16s} n={n:3d} | opp OBP {_m([r['obp'] for r in rows]):.3f} | "
          f"opp K% {_m([r['k'] for r in rows]):.3f} | home {100*sum(r['home'] for r in rows)/n:3.0f}% | "
          f"pitcher-median-ER {_m([r['q'] for r in rows]):.2f} | line {_m([r['line'] for r in rows]):.2f} | "
          f"actual-ER {_m([r['er'] for r in rows]):.2f}")


def main():
    recs, lgobp, lgk = build()
    print(f"league OBP={lgobp:.3f}  league K%={lgk:.3f}  |  {len(recs)} ER games matched\n")
    unders = recs                                   # every game is a candidate UNDER
    win = [r for r in unders if r["er"] < r["line"]]
    push = [r for r in unders if r["er"] == r["line"]]
    loss = [r for r in unders if r["er"] > r["line"]]
    print(f"UNDER: {len(win)}-{len(loss)} (push {len(push)}), hit {100*len(win)/(len(win)+len(loss)):.0f}%\n")
    print("=== profile: UNDER winners vs losers ===")
    prof("WON (under)", win)
    prof("LOST (under)", loss)
    print("\n=== losers split by line value (where does the under break?) ===")
    for ln in (1.5, 2.5, 3.5):
        grp = [r for r in unders if r["line"] == ln]
        if grp:
            w = sum(1 for r in grp if r["er"] < ln); l = sum(1 for r in grp if r["er"] > ln)
            print(f"  line {ln}: {w}-{l}  ({100*w/(w+l) if w+l else 0:.0f}% under)  n={len(grp)}")
    print("\n=== losers split by opponent offense (OBP terciles) ===")
    obps = sorted(v for v in [r['obp'] for r in recs] if v is not None)
    lo, hi = obps[len(obps)//3], obps[2*len(obps)//3]
    for tag, pred in (("weak (low OBP)", lambda r: r['obp'] is not None and r['obp'] <= lo),
                      ("mid", lambda r: r['obp'] is not None and lo < r['obp'] < hi),
                      ("strong (high OBP)", lambda r: r['obp'] is not None and r['obp'] >= hi)):
        grp = [r for r in unders if pred(r)]
        w = sum(1 for r in grp if r["er"] < r["line"]); l = sum(1 for r in grp if r["er"] > r["line"])
        print(f"  vs {tag:18s}: {w}-{l}  ({100*w/(w+l) if w+l else 0:.0f}% under)  n={len(grp)}")
    print("\n=== the loss tail: how bad are the blowups? ===")
    blow = [r for r in loss]
    print(f"  when the under LOSES, actual ER: mean {_m([r['er'] for r in blow]):.2f}, "
          f"max {max((r['er'] for r in blow), default=0)}, "
          f">=5ER: {sum(1 for r in blow if r['er']>=5)}/{len(blow)}")


if __name__ == "__main__":
    main()
