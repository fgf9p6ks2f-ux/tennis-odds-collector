"""Self-contained MLB K-prop edge backtest for GitHub Actions.

Reads the collected Pinnacle K-prop closing lines (mlb_kprops.sqlite) + realized
strikeouts (free MLB box scores), and reports whether our projection beats the sharp
line. Writes edge_report.md and the run summary. Runs on GitHub (no local machine).
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from mlb import data, project, strikeouts  # noqa: E402

DB = HERE / "mlb_kprops.sqlite"
REPORT = HERE / "edge_report.md"
EPS = 1e-6


def fair_prob(o1, o2):
    """Shin de-vig -> fair P(over)."""
    if not (o1 and o2) or o1 <= 1 or o2 <= 1:
        return np.nan
    pi1, pi2 = 1 / o1, 1 / o2
    bs = pi1 + pi2
    disc = max(bs * bs - 4 * (bs - 1) * (pi1 * pi1 + pi2 * pi2) / bs, 0.0)
    z = (bs - np.sqrt(disc)) / (bs - 2) if bs != 2 else 0.0
    z = min(max(z, 0.0), 0.5)
    return float((np.sqrt(z * z + 4 * (1 - z) * pi1 * pi1 / bs) - z) / (2 * (1 - z)))


def load_closing():
    if not DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(DB)
    df = pd.read_sql("SELECT * FROM k_props", con)
    con.close()
    if df.empty:
        return df
    df["collected_at"] = pd.to_datetime(df["collected_at"])
    df["start"] = pd.to_datetime(df["start_time"], errors="coerce", utc=True).dt.tz_localize(None)
    pre = df[df["collected_at"] <= df["start"]] if df["start"].notna().any() else df
    return pre.sort_values("collected_at").drop_duplicates(["pitcher", "line", "start"], keep="last")


def write(text):
    REPORT.write_text(text)
    print(text)


def main():
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    clo = load_closing()
    if clo.empty:
        write(f"# MLB K-prop edge report\n\n_{now}_\n\nNo props collected yet.")
        return
    season = int(clo["start"].dt.year.mode()[0])
    team_k, lg_k = data.team_kpct(season)
    team_kh, _ = data.team_kpct_by_hand(season)
    whiff = data.savant_whiff(season - 1)
    wa, wb = project.whiff_fit(whiff)

    rows = []
    for pitcher, g in clo.groupby("pitcher"):
        pid = data.find_pitcher(pitcher)
        if not pid:
            continue
        starts = [x for x in data.pitcher_gamelog(pid, season) if x["bf"]]
        if len(starts) < 3:
            continue
        by_date = {str(x["date"]): x for x in starts}
        k_sum = sum(x["k"] for x in starts)
        bf_sum = sum(x["bf"] for x in starts)
        wpr = project.whiff_prior(pid, whiff, wa, wb, lg_k)
        hand = data.pitcher_hand(pid)
        for _, r in g.iterrows():
            d = str(r["start"].date()) if pd.notna(r["start"]) else None
            gm = by_date.get(d)
            if not gm:
                continue
            opp = gm.get("opp_id")
            oppk = team_kh.get(opp, {}).get(hand, team_k.get(opp, lg_k))
            mean_k = project.project_mean(k_sum, bf_sum, len(starts), oppk, lg_k, wpr)
            our = strikeouts.prob_over(mean_k, r["line"])
            pin = fair_prob(r["over_odds"], r["under_odds"])
            if pin != pin:
                continue
            rows.append({"over": int(gm["k"] > r["line"]), "our": our, "pin": pin,
                         "oo": r["over_odds"], "uo": r["under_odds"]})

    df = pd.DataFrame(rows)
    if df.empty:
        n_open = len(clo)
        write(f"# MLB K-prop edge report\n\n_{now}_\n\n{n_open} prop lines collected; "
              f"none settled yet — check back after games finish.")
        return
    y = df["over"].to_numpy()

    def ll(p):
        p = np.clip(p, EPS, 1 - EPS)
        return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))

    # ROI betting our +EV side into Pinnacle's price
    staked = pnl = 0.0
    for _, r in df.iterrows():
        if r["our"] * r["oo"] > 1:
            staked += 1; pnl += (r["oo"] - 1) if r["over"] else -1
        if (1 - r["our"]) * r["uo"] > 1:
            staked += 1; pnl += (r["uo"] - 1) if not r["over"] else -1
    roi = (pnl / staked * 100) if staked else float("nan")
    verdict = "MODEL beats the sharp line" if ll(df["our"]) < ll(df["pin"]) else "Pinnacle wins"
    shade = y.mean() - df["pin"].mean()
    write(
        f"# MLB K-prop edge report\n\n_{now}_ — {len(df)} settled prop lines\n\n"
        f"| metric | value |\n|---|---|\n"
        f"| log-loss (our model) | {ll(df['our']):.4f} |\n"
        f"| log-loss (Pinnacle) | {ll(df['pin']):.4f} |\n"
        f"| verdict | **{verdict}** |\n"
        f"| actual over-rate | {y.mean():.3f} |\n"
        f"| Pinnacle-implied over | {df['pin'].mean():.3f} |\n"
        f"| over-shading (actual − implied) | {shade:+.3f} ({'lines shaded UNDER' if shade>0 else 'shaded OVER'}) |\n"
        f"| ROI betting our edge into Pinnacle | {roi:+.2f}% ({int(staked)} bets) |\n\n"
        f"Small samples are noisy — trust this once it's a few hundred settled lines.\n")


if __name__ == "__main__":
    main()
