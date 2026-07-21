import sqlite3, dashboard as D

con = sqlite3.connect(f"file:{D.FD_PROPS}?mode=ro", uri=True)
rows = con.execute(
    "SELECT book, player, event, stat, line, side, odds, collected_at FROM fd_lines "
    "WHERE sport='mlb' AND stat IN ('outs','strikeouts') "
    "AND collected_at > datetime('now','-18 hours')").fetchall()
con.close()
latest, ev_by = {}, {}
for bk, pl, ev, stat, line, side, odds, cat in rows:
    if odds is None or line is None or side is None:
        continue
    k = (bk or "fd", pl, stat, round(float(line), 1), side)
    if k not in latest or cat > latest[k][1]:
        latest[k] = (float(odds), cat)
    ev_by[(pl, stat)] = ev
ladder = {}
for (bk, pl, stat, line, side), (odds, _c) in latest.items():
    ladder.setdefault((bk, pl, stat), {}).setdefault(line, {})[side] = odds
mains = {}
for (bk, pl, stat), lines in ladder.items():
    two = {ln: v for ln, v in lines.items() if "over" in v and "under" in v} or lines
    main = min(two, key=lambda ln: abs((two[ln].get("over") or two[ln].get("under") or 9) - 1.95))
    mains.setdefault((pl, stat), []).append((bk, main, two[main].get("over"), two[main].get("under")))

hit = D._team_hit()
ppa_low = D._ppa_low(hit)
print(f"CONTACT_MAX={D.CONTACT_MAX} OUTS_UNDER_MAX={D.OUTS_UNDER_MAX} ppa_low={ppa_low:.3f}\n")
drop = {"not_outs":0,"not_away":0,"contact_fail":0,"no_cand_le_max":0,"not_premium":0,"PASS":0}
for (pl, stat), offs in sorted(mains.items()):
    if stat != "outs":
        drop["not_outs"]+=1; continue
    ev = ev_by.get((pl, stat), "")
    away, opp = D._mlb_matchup(ev, pl)
    oh = hit.get((opp or "").lower(), {})
    oppk = oh.get("k")
    if not away:
        drop["not_away"]+=1
        print(f"  DROP not_away  {pl:20s} ev={ev!r} -> away={away} opp={opp!r}")
        continue
    if hit and oppk is not None and oppk >= D.CONTACT_MAX:
        drop["contact_fail"]+=1
        print(f"  DROP contact   {pl:20s} @{opp} oppk={oppk:.3f}")
        continue
    cands = [(bk, ln, uo) for bk, ln, oo, uo in offs if uo and ln <= D.OUTS_UNDER_MAX]
    if not cands:
        drop["no_cand_le_max"]+=1
        print(f"  DROP no<=16.5  {pl:20s} @{opp} lines={[ln for _,ln,_,_ in offs]}")
        continue
    cands.sort(key=lambda x: (x[1], x[2]), reverse=True)
    bk, line, odds = cands[0]
    oppp = oh.get("ppa")
    r5 = D._pitcher_r5(pl)
    premium = bool((oppp is not None and oppp < ppa_low) or (r5 is not None and line > r5))
    if not premium:
        drop["not_premium"]+=1
        print(f"  DROP not_prem  {pl:20s} @{opp} line={line:g} oppp={oppp} r5={r5}")
        continue
    drop["PASS"]+=1
    print(f"  PASS           {pl:20s} @{opp} U{line:g} oppp={oppp} r5={r5}")
print("\nSUMMARY:", drop)
