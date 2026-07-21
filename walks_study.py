"""Walks PREDICTABILITY study (NOT a betting backtest). There are no real historical walk lines
(Pinnacle doesn't post them; DK started today), so per the 'real lines only' rule this reports
HIT RATES at the common proxy lines (0.5/1.5/2.5) + what SHIFTS them — never a fake ROI, because
without real per-pitcher lines and juice an ROI would be fiction. Purpose: form the hypotheses the
forward DK walks_paper tracker will actually test. Pool = every start of every pitcher who shows up
in our prop data. Signals: recent-5 walk median (transferable 'line>recent'), opponent patience
(pitches/PA) + OBP, home/away."""
import sqlite3
import statistics as st
from mlb import data as D
import k_paper

PROXY = [0.5, 1.5, 2.5]


def pitchers():
    c = sqlite3.connect("mlb_kprops.sqlite")
    out = [r[0] for r in c.execute("SELECT DISTINCT pitcher FROM pitcher_props")]
    c.close()
    return out


def main():
    season = 2026
    tk, lgk = D.team_kpct(season)
    tobp, lgobp = D.team_obp(season)
    _tk, _lg, ppa, _p25 = k_paper._team_hit(season)
    starts = []
    idc = k_paper._load_ids()
    for name in pitchers():
        pid = idc.get(name) or D.find_pitcher(name)
        idc[name] = pid
        if not pid:
            continue
        try:
            gl = sorted([g for g in D.pitcher_gamelog(pid, season) if g.get("bb") is not None],
                        key=lambda g: g.get("date") or "")
        except Exception:
            continue
        for i, g in enumerate(gl):
            prv = [x["bb"] for x in gl[:i]][-5:]
            starts.append({"bb": g["bb"], "home": bool(g.get("is_home")), "opp": g.get("opp_id"),
                           "r5": st.median(prv) if len(prv) >= 3 else None})
    n = len(starts)
    print(f"{n} starts pooled | league OBP {lgobp:.3f}\n")

    print("=== BASE RATES (share of starts UNDER each proxy line) ===")
    for ln in PROXY:
        u = sum(1 for s in starts if s["bb"] < ln)
        print(f"  under {ln}: {100*u/n:4.1f}%  ({u}/{n})   [avg bb/start = {st.mean(s['bb'] for s in starts):.2f}]"
              if ln == PROXY[0] else f"  under {ln}: {100*u/n:4.1f}%  ({u}/{n})")

    print("\n=== transferable signal: does 'proxy line > recent-5 median walks' -> more UNDERS? ===")
    seg = [s for s in starts if s["r5"] is not None]
    for ln in PROXY:
        above = [s for s in seg if ln > s["r5"]]
        below = [s for s in seg if ln <= s["r5"]]
        ua = sum(1 for s in above if s["bb"] < ln); ub = sum(1 for s in below if s["bb"] < ln)
        print(f"  line {ln}: line>recent -> under {100*ua/len(above) if above else 0:4.1f}% (n={len(above)})   "
              f"| line<=recent -> under {100*ub/len(below) if below else 0:4.1f}% (n={len(below)})")

    print("\n=== opponent patience (pitches/PA) — do patient offenses draw more walks? ===")
    pv = sorted(v for v in ppa.values())
    plo, phi = pv[len(pv)//3], pv[2*len(pv)//3]
    for tag, pred in (("PATIENT opp (top ppa)", lambda s: s["opp"] in ppa and ppa[s["opp"]] >= phi),
                      ("impatient opp (bot ppa)", lambda s: s["opp"] in ppa and ppa[s["opp"]] <= plo)):
        grp = [s for s in starts if pred(s)]
        avg = st.mean(s["bb"] for s in grp) if grp else 0
        u15 = sum(1 for s in grp if s["bb"] < 1.5)
        print(f"  {tag:24s}: avg bb {avg:.2f} | under1.5 {100*u15/len(grp) if grp else 0:4.1f}% (n={len(grp)})")

    print("\n=== home/away ===")
    for tag, pred in (("home", lambda s: s["home"]), ("away", lambda s: not s["home"])):
        grp = [s for s in starts if pred(s)]
        print(f"  {tag}: avg bb {st.mean(s['bb'] for s in grp):.2f} | "
              f"under1.5 {100*sum(1 for s in grp if s['bb']<1.5)/len(grp):4.1f}% (n={len(grp)})")


if __name__ == "__main__":
    main()
