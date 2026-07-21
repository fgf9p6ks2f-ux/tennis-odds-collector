"""Earned-runs predictability study (mirrors the outs edge hunt). Pinnacle-only lines (no soft book
posts pitcher ER), so this asks ONE thing: does any SEGMENT beat Pinnacle's CLOSING ER number? If
not, there's no signal to chase and no market to bet it in anyway. ROI = decimal_odds-based."""
import sqlite3, datetime as dt, statistics as st
from mlb import data as D

PROPS = "mlb_kprops.sqlite"


def _dec(o):
    if o is None:
        return None
    o = float(o)
    if o >= 2 or (0 < o < 2):        # already decimal (Pinnacle stores decimal like 1.90)
        return o if o > 1 else None
    return None


def load_props(stat):
    c = sqlite3.connect(PROPS)
    # dedupe to the CLOSING snapshot per pitcher-game (latest collected_at before/at start)
    rows = c.execute(
        "SELECT pitcher, date(start_time) gd, line, over_odds, under_odds, collected_at, start_time "
        "FROM pitcher_props WHERE stat=? ORDER BY collected_at", (stat,)).fetchall()
    close = {}
    for pit, gd, line, oo, uo, cat, stt in rows:
        close[(pit, gd)] = (line, oo, uo, stt)      # last wins = closing
    return close


def match_actual(pit, gd, glcache, idcache):
    pid = idcache.get(pit)
    if pid is None and pit not in idcache:
        pid = D.find_pitcher(pit); idcache[pit] = pid
    if not pid:
        return None
    if pid not in glcache:
        try:
            glcache[pid] = D.pitcher_gamelog(pid, int(gd[:4]))
        except Exception:
            glcache[pid] = []
    try:
        ld = dt.date.fromisoformat(gd)
    except ValueError:
        return None
    best, g = None, None
    for x in glcache[pid]:
        if not x.get("date"):
            continue
        try:
            diff = abs((dt.date.fromisoformat(x["date"]) - ld).days)
        except ValueError:
            continue
        if diff <= 1 and (best is None or diff < best):
            best, g = diff, x
    return g


def roi(bets):
    """bets = list of (side, line, actual, dec_odds). Returns (n, w, roi%)."""
    n = w = 0; pnl = 0.0
    for side, line, actual, dec in bets:
        if actual == line or dec is None:
            continue
        won = (actual > line) if side == "over" else (actual < line)
        n += 1; w += won
        pnl += (dec - 1) if won else -1
    return n, w, (100 * pnl / n if n else 0.0)


def main():
    season = 2026
    tk, lg = D.team_kpct(season)          # {team_id: K%/PA}
    tobp, lgobp = D.team_obp(season)      # {team_id: OBP}
    close = load_props("earned_runs")
    glcache, idcache = {}, {}
    recs = []
    for (pit, gd), (line, oo, uo, stt) in close.items():
        g = match_actual(pit, gd, glcache, idcache)
        if not g or g.get("er") is None:
            continue
        recs.append({"pit": pit, "gd": gd, "line": float(line), "oo": _dec(oo), "uo": _dec(uo),
                     "er": g["er"], "home": bool(g.get("is_home")), "opp": g.get("opp_id"),
                     "pid": idcache.get(pit)})
    print(f"matched {len(recs)} pitcher-games with a real ER result\n")

    # recent-ER median per pitcher (point-in-time: starts strictly before this game)
    for r in recs:
        gl = [x for x in glcache.get(r["pid"], []) if x.get("date") and x["date"] < r["gd"]
              and x.get("er") is not None]
        gl = sorted(gl, key=lambda x: x["date"])[-5:]
        r["r5"] = st.median(x["er"] for x in gl) if len(gl) >= 3 else None

    def seg(name, pred):
        for side in ("over", "under"):
            bets = [(side, r["line"], r["er"], r["oo"] if side == "over" else r["uo"])
                    for r in recs if pred(r)]
            n, w, rp = roi(bets)
            if n >= 15:
                print(f"  {name:34s} {side:5s}  {w:3d}-{n-w:<3d} {100*w/n:4.0f}%  ROI {rp:+6.1f}%  (n={n})")

    print("=== BASELINE (all matched) ===")
    seg("all", lambda r: True)
    print("\n=== by home/away ===")
    seg("home starter", lambda r: r["home"])
    seg("away starter", lambda r: not r["home"])
    print("\n=== by opponent offense (OBP terciles) ===")
    obps = sorted(v for v in tobp.values())
    lo, hi = obps[len(obps)//3], obps[2*len(obps)//3]
    seg("vs STRONG offense (top OBP)", lambda r: r["opp"] in tobp and tobp[r["opp"]] >= hi)
    seg("vs WEAK offense (bot OBP)", lambda r: r["opp"] in tobp and tobp[r["opp"]] <= lo)
    print("\n=== by opponent contact (K% terciles) ===")
    ks = sorted(v for v in tk.values())
    klo, khi = ks[len(ks)//3], ks[2*len(ks)//3]
    seg("vs CONTACT offense (low K%)", lambda r: r["opp"] in tk and tk[r["opp"]] <= klo)
    seg("vs WHIFF offense (high K%)", lambda r: r["opp"] in tk and tk[r["opp"]] >= khi)
    print("\n=== by recent form (line vs recent-5 median ER) ===")
    seg("line ABOVE recent ER", lambda r: r["r5"] is not None and r["line"] > r["r5"])
    seg("line BELOW recent ER", lambda r: r["r5"] is not None and r["line"] < r["r5"])
    print("\n=== stacks (transfer of the outs edge) ===")
    seg("away + strong offense", lambda r: not r["home"] and r["opp"] in tobp and tobp[r["opp"]] >= hi)
    seg("away + contact", lambda r: not r["home"] and r["opp"] in tk and tk[r["opp"]] <= klo)


if __name__ == "__main__":
    main()
