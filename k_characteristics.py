"""Where is the strikeout model actually RELIABLE? — the conditional-edge study.

Predicting every pitcher's Ks is a coin flip (proj-vs-actual corr 0.37). The smarter
question: is there a SUBSET of starts — defined by pitcher / matchup characteristics —
where the model is sharp, or where actual Ks systematically beat/miss the projection?
Those buckets are where a bet is worth making.

Walk-forward on free statsapi gamelogs (no leak): project each start from the pitcher's
PRIOR starts + opponent team K%, record its characteristics, then bucket and measure:
  bias    = mean(actual - projected)   (systematic over/under -> a directional edge)
  |corr|  = projection skill in the bucket
  over%   = share of starts over the projection (50% = unbiased; far off = exploitable)

    python k_characteristics.py --seasons 2025,2026 --pitchers 150
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import numpy as np

from mlb import data


def starts_with_features(pid, season, tk, lg_k):
    """Yield per-start dicts: proj, actual, and characteristics, walk-forward."""
    logs = data.pitcher_gamelog(pid, season)
    k_sum = bf_sum = n = 0
    recent = []                                       # last-3 K counts
    prev_date = None
    out = []
    for g in logs:
        bf, k = g["bf"], g["k"]
        if bf < 5:
            continue
        if n >= 3:
            k_pct = (k_sum + 120 * 0.22) / (bf_sum + 120)
            exp_bf = float(np.clip(bf_sum / n, 18, 27))
            opp = tk.get(g["opp_id"], lg_k)
            a = k_pct * opp / lg_k
            b = (1 - k_pct) * (1 - opp) / (1 - lg_k)
            proj = a / (a + b) * exp_bf
            try:
                import datetime as dt
                d = dt.date.fromisoformat(g["date"])
                rest = (d - prev_date).days if prev_date else 5
            except (ValueError, TypeError):
                rest = 5
            out.append({"proj": proj, "actual": k, "k_pct": k_pct, "opp_k": opp,
                        "exp_bf": exp_bf, "is_home": 1 if g.get("is_home") else 0,
                        "rest": rest, "recent_mean": np.mean(recent[-3:]) if recent else k,
                        "pitches": g.get("pitches") or 0})
        k_sum += k; bf_sum += bf; n += 1
        recent.append(k)
        try:
            import datetime as dt
            prev_date = dt.date.fromisoformat(g["date"])
        except (ValueError, TypeError):
            pass
    return out


def report_bucket(title, groups):
    print(f"\n=== {title} ===")
    print(f"{'bucket':>16}{'n':>6}{'proj':>7}{'actual':>8}{'bias':>7}{'over%':>7}{'|corr|':>8}")
    for label in sorted(groups):
        rows = groups[label]
        if len(rows) < 60:
            continue
        proj = np.array([r["proj"] for r in rows])
        act = np.array([r["actual"] for r in rows])
        over = np.mean(act > proj) * 100
        corr = abs(np.corrcoef(proj, act)[0, 1]) if len(rows) > 2 else 0
        print(f"{label:>16}{len(rows):>6}{proj.mean():>7.2f}{act.mean():>8.2f}"
              f"{(act-proj).mean():>+7.2f}{over:>6.0f}%{corr:>8.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2025,2026")
    ap.add_argument("--pitchers", type=int, default=150)
    args = ap.parse_args()
    rows = []
    for season in [int(s) for s in args.seasons.split(",")]:
        tk, lg_k = data.team_kpct(season)
        ids = data.starter_ids(season, limit=args.pitchers)
        for i, pid in enumerate(ids):
            try:
                rows += starts_with_features(pid, season, tk, lg_k)
            except Exception:
                continue
            if i % 40 == 0:
                print(f"  {season}: {i}/{len(ids)}, {len(rows)} starts", flush=True)
    print(f"\n{len(rows)} projected starts. Overall bias "
          f"{np.mean([r['actual']-r['proj'] for r in rows]):+.2f}, "
          f"corr {np.corrcoef([r['proj'] for r in rows], [r['actual'] for r in rows])[0,1]:.3f}")

    def bucket(fn):
        g = defaultdict(list)
        for r in rows:
            g[fn(r)].append(r)
        return g

    report_bucket("by projected K (model's own confidence)",
                  bucket(lambda r: f"{int(r['proj'])}-{int(r['proj'])+1}"))
    report_bucket("by opponent K% (weak/strong-K lineup)",
                  bucket(lambda r: "high-K opp" if r["opp_k"] >= 0.235
                         else "low-K opp" if r["opp_k"] < 0.205 else "mid"))
    report_bucket("by pitcher K% (ace vs contact)",
                  bucket(lambda r: "ace >27%" if r["k_pct"] >= 0.27
                         else "low <20%" if r["k_pct"] < 0.20 else "mid"))
    report_bucket("home / away", bucket(lambda r: "home" if r["is_home"] else "away"))
    report_bucket("days rest", bucket(lambda r: "extra 6+" if r["rest"] >= 6
                                      else "short <=4" if r["rest"] <= 4 else "normal 5"))
    report_bucket("ace vs high-K lineup (the smash spot)",
                  bucket(lambda r: "ace+highK" if (r["k_pct"] >= 0.27 and r["opp_k"] >= 0.235)
                         else "other"))


if __name__ == "__main__":
    main()
