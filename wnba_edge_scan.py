"""Self-contained WNBA FanDuel-vs-Pinnacle edge scan for GitHub Actions (stdlib only).

Runs each collection cycle. Reads the latest Pinnacle (wnba_props) and FanDuel (fd_lines,
sport=wnba) snapshots, de-vigs Pinnacle to a fair probability, and flags where FanDuel's
price beats it (+EV => positive CLV). Two ways to match:
  * direct — FD line == a Pinnacle line (model-free, sharpest).
  * model  — anchor a Normal to Pinnacle's MAIN line, price FD's alt lines (points /
             rebounds / assists, each with its own game-to-game sd curve).
Only pre-game lines are compared (a started game's line is stale). Writes wnba_edge_report.md.

Mirrors the main project's fd_edge_scan.py; the model + sd curves are inlined so the
collector workflow needs no numpy/pandas.
"""
import datetime as dt
import sqlite3
import unicodedata
from math import sqrt
from pathlib import Path
from statistics import NormalDist

HERE = Path(__file__).resolve().parent
PINN_DB = HERE / "wnba_props.sqlite"
FD_DB = HERE / "fanduel_props.sqlite"
REPORT = HERE / "wnba_edge_report.md"

# per-stat game-to-game sd ~ a + b*mean, fit from 2025 game logs (see wnba_fit_sd.py)
SD_CURVES = {"points": (3.64, 0.184), "rebounds": (1.11, 0.280), "assists": (0.83, 0.315)}
MIN_EV = 0.02          # flag FanDuel prices at least this much better than sharp fair
_N = NormalDist()


def canon(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return "".join(c for c in s.lower() if c.isalpha())


def fair_prob(o1, o2):
    """Shin de-vig -> fair P(over) from two-sided decimal odds."""
    if not (o1 and o2) or o1 <= 1 or o2 <= 1:
        return None
    pi1, pi2 = 1 / o1, 1 / o2
    bs = pi1 + pi2
    if bs == 2:
        z = 0.0
    else:
        disc = max(bs * bs - 4 * (bs - 1) * (pi1 * pi1 + pi2 * pi2) / bs, 0.0)
        z = min(max((bs - sqrt(disc)) / (bs - 2), 0.0), 0.5)
    return (sqrt(z * z + 4 * (1 - z) * pi1 * pi1 / bs) - z) / (2 * (1 - z))


def prob_over(mean, line, sd):
    return 1 - _N.cdf((line - mean) / max(sd, 1e-6))


def anchor(line0, p0, stat):
    """(mean, sd) so a Normal reproduces Pinnacle's P(over line0)=p0; sd from the stat's
    curve. mean = line0 - sd * Phi^{-1}(1 - p0)."""
    a, b = SD_CURVES[stat]
    sd = a + b * line0
    z = _N.inv_cdf(min(max(1 - p0, 1e-6), 1 - 1e-6))
    return line0 - sd * z, sd


def _latest(db, table, cols, where=""):
    if not db.exists():
        return None, []
    con = sqlite3.connect(db)
    try:
        mx = con.execute(f"SELECT max(collected_at) FROM {table}").fetchone()[0]
        rows = con.execute(f"SELECT {cols} FROM {table} WHERE collected_at=?{where}",
                           (mx,)).fetchall() if mx else []
    except sqlite3.OperationalError:
        mx, rows = None, []
    con.close()
    return mx, rows


def scan():
    psnap, prows = _latest(PINN_DB, "wnba_props",
                           "player,stat,line,over_odds,under_odds,start_time")
    fsnap, frows = _latest(FD_DB, "fd_lines", "player,stat,line,side,odds",
                           where=" AND sport='wnba'")
    if not prows:
        return psnap, fsnap, None, "No Pinnacle WNBA props in the latest snapshot — props " \
            "post gameday; the scan fills in once they're up."
    # staleness guard: keep only pre-game Pinnacle lines (start_time > snapshot, UTC prefix)
    snap19 = str(psnap)[:19]
    live = [r for r in prows if r[5] and str(r[5])[:19] > snap19]
    if not live:
        return psnap, fsnap, None, "All Pinnacle WNBA lines are for games already started " \
            "— nothing pre-game to compare. (Edges are only valid before tip.)"
    ref, anchors = {}, {s: {} for s in SD_CURVES}
    for player, stat, line, oo, uo, _ in live:
        p = fair_prob(oo, uo)
        if p is None:
            continue
        ref[(canon(player), stat, round(float(line), 1))] = p
        if stat in anchors:
            anchors[stat][canon(player)] = (round(float(line), 1), p)

    flagged = []
    for player, stat, line, side, odds in frows:
        L = round(float(line), 1) if line is not None else None
        p = ref.get((canon(player), stat, L))
        src = "direct"
        if p is not None:
            p_side = p if side == "over" else 1 - p
        elif side == "over" and stat in anchors and canon(player) in anchors[stat]:
            line0, p0 = anchors[stat][canon(player)]
            if L == line0:
                continue
            mean, sd = anchor(line0, p0, stat)
            p_side, src = prob_over(mean, L, sd), "model"
        else:
            continue
        ev = p_side * float(odds) - 1
        if ev >= MIN_EV:
            flagged.append((ev, player, stat, line, side, odds, p_side, src))
    flagged.sort(reverse=True)
    return psnap, fsnap, flagged, None


def main():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    psnap, fsnap, flagged, note = scan()
    lines = [f"# WNBA prop edge report", "", f"_{now}_", "",
             f"Pinnacle snapshot: `{psnap}`  |  FanDuel snapshot: `{fsnap}`", ""]
    if note:
        lines += [note, ""]
    elif not flagged:
        lines += ["Pre-game lines matched, but no FanDuel price beats the sharp fair value "
                  f"by ≥{MIN_EV*100:.0f}% right now. (This is the usual state — edges are "
                  "occasional.)", ""]
    else:
        lines += [f"**{len(flagged)} +EV bets** (FanDuel price beats Pinnacle's de-vigged "
                  "fair value => positive CLV):", "",
                  "| player | stat | line | side | FD odds | sharp fair | EV% | src |",
                  "|---|---|---|---|---|---|---|---|"]
        for ev, pl, st, ln, sd, od, ps, src in flagged[:40]:
            lines.append(f"| {pl} | {st} | {ln} | {sd} | {od:.2f} | {ps:.3f} | "
                         f"{ev*100:+.1f}% | {src} |")
        lines += ["", "Bet the +EV side at FanDuel. `direct` = FD line matched a Pinnacle "
                  "line; `model` = Pinnacle-anchored Normal priced an alt line."]
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
