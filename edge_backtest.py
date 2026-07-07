"""Self-contained MLB prop edge backtest for GitHub Actions — strikeouts AND outs.

Reads collected Pinnacle prop closing lines (pitcher_props) + realized box scores, and
grades our projection vs the sharp line per stat. Writes edge_report.md + the run summary.
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from mlb import data, outs, project, strikeouts  # noqa: E402

DB = HERE / "mlb_kprops.sqlite"
REPORT = HERE / "edge_report.md"
EPS = 1e-6
OUTS_SD = 3.6


def fair_prob(o1, o2):
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
    try:
        df = pd.read_sql("SELECT * FROM pitcher_props", con)
    except Exception:
        df = pd.DataFrame()
    con.close()
    if df.empty:
        return df
    df["collected_at"] = pd.to_datetime(df["collected_at"])
    df["start"] = pd.to_datetime(df["start_time"], errors="coerce", utc=True).dt.tz_localize(None)
    df = df.sort_values("collected_at")
    key = ["pitcher", "stat", "line", "start"]
    pre = df[df["collected_at"] <= df["start"]].drop_duplicates(key, keep="last")
    last = df.drop_duplicates(key, keep="last")
    return pd.concat([pre, last]).drop_duplicates(key, keep="first").reset_index(drop=True)


def grade_stat(sub, season, inp):
    rows = []
    for pitcher, g in sub.groupby("pitcher"):
        pid = data.find_pitcher(pitcher)
        if not pid:
            continue
        log = [x for x in data.pitcher_gamelog(pid, season) if x["bf"]]
        if len(log) < 3:
            continue
        by_date = {str(x["date"]): x for x in log}
        n = len(log)
        k_sum = sum(x["k"] for x in log); bf_sum = sum(x["bf"] for x in log)
        outs_sum = sum(x["outs"] for x in log)
        wpr = project.whiff_prior(pid, inp["whiff"], inp["wa"], inp["wb"], inp["lg_k"])
        hand = data.pitcher_hand(pid)
        for _, r in g.iterrows():
            gm = by_date.get(str(r["start"].date())) if pd.notna(r["start"]) else None
            if not gm:
                continue
            opp = gm.get("opp_id")
            if r["stat"] == "strikeouts":
                oppk = inp["team_kh"].get(opp, {}).get(hand, inp["team_k"].get(opp, inp["lg_k"]))
                mean = project.project_mean(k_sum, bf_sum, n, oppk, inp["lg_k"], wpr)
                our, realized = strikeouts.prob_over(mean, r["line"]), gm["k"]
            else:
                mean = outs.project(outs_sum, n, opp_obp=inp["obp"].get(opp), lg_obp=inp["lg_obp"])
                our, realized = outs.prob_over(mean, r["line"], OUTS_SD), gm["outs"]
            pin = fair_prob(r["over_odds"], r["under_odds"])
            if pin != pin:
                continue
            rows.append({"over": int(realized > r["line"]), "our": our, "pin": pin,
                         "oo": r["over_odds"], "uo": r["under_odds"]})
    return pd.DataFrame(rows)


def section(stat, df):
    if df.empty:
        return f"### {stat}\n\nno settled lines yet.\n"
    y = df["over"].to_numpy()

    def ll(p):
        p = np.clip(p, EPS, 1 - EPS)
        return -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
    staked = pnl = 0.0
    for _, r in df.iterrows():
        if r["our"] * r["oo"] > 1:
            staked += 1; pnl += (r["oo"] - 1) if r["over"] else -1
        if (1 - r["our"]) * r["uo"] > 1:
            staked += 1; pnl += (r["uo"] - 1) if not r["over"] else -1
    roi = (pnl / staked * 100) if staked else float("nan")
    shade = y.mean() - df["pin"].mean()
    return (f"### {stat} — {len(df)} settled lines\n\n"
            f"| metric | value |\n|---|---|\n"
            f"| log-loss our / Pinnacle | {ll(df['our']):.4f} / {ll(df['pin']):.4f} "
            f"({'we beat sharp' if ll(df['our']) < ll(df['pin']) else 'Pinnacle wins'}) |\n"
            f"| over-shading (actual − implied) | {shade:+.3f} |\n"
            f"| ROI into Pinnacle | {roi:+.2f}% ({int(staked)} bets) |\n\n")


def main():
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    clo = load_closing()
    if clo.empty:
        REPORT.write_text(f"# MLB prop edge report\n\n_{now}_\n\nNo props collected yet.")
        print(REPORT.read_text()); return
    season = int(clo["start"].dt.year.mode()[0])
    team_k, lg_k = data.team_kpct(season)
    team_kh, _ = data.team_kpct_by_hand(season)
    whiff = data.savant_whiff(season - 1)
    wa, wb = project.whiff_fit(whiff)
    obp, lg_obp = data.team_obp(season)
    inp = dict(team_k=team_k, lg_k=lg_k, team_kh=team_kh, whiff=whiff, wa=wa, wb=wb,
               obp=obp, lg_obp=lg_obp)
    body = "".join(section(s, grade_stat(clo[clo["stat"] == s], season, inp))
                   for s in ("strikeouts", "outs"))
    text = (f"# MLB prop edge report\n\n_{now}_ — {len(clo)} prop lines collected\n\n"
            f"{body}\nSmall samples are noisy; trust once a few hundred lines settle.\n")
    REPORT.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
