"""Targeted test: HIGH-VOLUME 3PT shooters vs OPPONENT ARC DEFENSE — the cleanest place a real
zone-matchup effect should show (3PT shooting is the most matchup-sensitive; volume shooters
have stable rates; arc defense is a single well-sampled quantity).

Does a volume shooter's actual threes beat/miss their minutes-honest projection in the direction
the opponent's rolling 3PT-defense predicts? Box-score only (3PM/3PA), leak-free chronological.

    python arc_signal.py [test_days] [warmup_days] [min_3pa_per_game]
"""
import statistics as st
import sys
from collections import defaultdict

import gs_backtest as G
import wnba_backtest_layers as B
import wnba_dvp as D
import wnba_pbp as P


def boxrows(gid):
    box = P.fetch(gid).get("boxscore", {})
    teams, order, tm3 = {}, [], {}
    for tm in box.get("players", []):
        team = (tm.get("team") or {}).get("abbreviation")
        if team not in teams:
            teams[team], tm3[team] = {}, [0.0, 0.0]
            order.append(team)
        for stt in tm.get("statistics", []):
            keys = stt.get("keys") or []

            def gi(k):
                return keys.index(k) if k in keys else None

            im, ip = gi("minutes"), gi("points")
            i3 = gi("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
            for a in stt.get("athletes", []):
                pid = a.get("athlete", {}).get("id")
                s = a.get("stats") or []
                if not pid or not s:
                    continue

                def num(i):
                    try:
                        return float(s[i]) if i is not None else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                def ma(i):
                    try:
                        return float(s[i].split("-")[0]), float(s[i].split("-")[1])
                    except (ValueError, IndexError, AttributeError):
                        return 0.0, 0.0

                mn = num(im)
                m3, a3 = ma(i3)
                tm3[team][0] += m3
                tm3[team][1] += a3
                if mn > 0:
                    teams[team][pid] = {"min": mn, "pts": num(ip), "fg3m": m3, "fg3a": a3}
    if len(order) != 2:
        return {}, {}
    a, b = order
    rows = {pid: {**r, "team": team, "opp": opp}
            for team, opp in ((a, b), (b, a)) for pid, r in teams[team].items()}
    return rows, tm3


def corr_t(pairs):
    n = len(pairs)
    if n < 20:
        return 0, 0, 0, n
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    r = cov / (vx * vy) ** 0.5 if vx and vy else 0
    beta = cov / vx if vx else 0
    t = r * ((n - 2) / (1 - r * r)) ** 0.5 if abs(r) < 1 else 0
    return r, beta, t, n


def main():
    test_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    warmup = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    min3pa = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    _id2pos, wnba = D.positions()
    now = B.dt.datetime.now(B.dt.timezone.utc).astimezone(B.dt.timezone(B.dt.timedelta(hours=-4)))
    tstart = (now - B.dt.timedelta(days=test_days)).strftime("%Y-%m-%d")

    games = sorted(B.game_ids(test_days + warmup + 4), key=lambda gd: gd[1])
    hist = defaultdict(list)
    allowed = defaultdict(lambda: [0.0, 0.0])       # team -> [opp 3PM allowed, opp 3PA allowed]
    lg = [0.0, 0.0]
    adj_p, raw_p, pts_p = [], [], []                # (signal, residual) pairs
    deltas = []
    for gid, date in games:
        try:
            rows, tm3 = boxrows(gid)
        except Exception:
            continue
        if not rows:
            continue
        if date >= tstart:
            lg3 = lg[0] / lg[1] if lg[1] else 0.34
            for pid, r in rows.items():
                if r["team"] not in wnba or r["opp"] not in wnba or len(hist[pid]) < 5:
                    continue
                fg3a_pg = st.mean(x["fg3a"] for x in hist[pid])
                if fg3a_pg < min3pa:               # HIGH-VOLUME shooters only
                    continue
                proj_min = st.mean(x["min"] for x in hist[pid][-5:])
                mh3 = G.elev_proj(hist[pid], proj_min, "fg3m")
                mhp = G.elev_proj(hist[pid], proj_min, "pts")
                al = allowed[r["opp"]]
                if al[1] < 30 or mh3 is None:       # need a real defensive sample
                    continue
                arc_delta = al[0] / al[1] - lg3     # opp allowed 3PT% minus league
                deltas.append(arc_delta)
                adj = fg3a_pg * arc_delta            # expected extra 3PM from the matchup
                adj_p.append((adj, r["fg3m"] - mh3))
                raw_p.append((arc_delta, r["fg3m"] - mh3))
                if mhp is not None:
                    pts_p.append((3 * adj, r["pts"] - mhp))
        for pid, r in rows.items():
            hist[pid].append(r)
        a, b = list(tm3)
        allowed[a][0] += tm3[b][0]
        allowed[a][1] += tm3[b][1]
        allowed[b][0] += tm3[a][0]
        allowed[b][1] += tm3[a][1]
        lg[0] += tm3[a][0] + tm3[b][0]
        lg[1] += tm3[a][1] + tm3[b][1]

    n = len(adj_p)
    print(f"\nARC-DEFENSE x HIGH-VOLUME 3PT SHOOTERS — {n} shooter-games (>= {min3pa:.0f} 3PA/g), "
          f"rolling season-long arc D")
    if n < 20:
        print("  too few high-volume-shooter spots on this window."); return
    print(f"  opponent arc-defense spread: sd {st.pstdev(deltas)*100:.1f} pct-pts "
          f"(range {min(deltas)*100:+.0f}..{max(deltas)*100:+.0f})\n")
    for label, pairs, unit in (("3PM resid ~ volume x arc_delta", adj_p, "3PM"),
                               ("3PM resid ~ raw arc_delta     ", raw_p, "3PM"),
                               ("PTS resid ~ 3 x volume x delta ", pts_p, "pts")):
        r, beta, t, nn = corr_t(pairs)
        sig = "  << REAL (|t|>2)" if abs(t) > 2 else ""
        print(f"  {label}:  r={r:+.3f}  t={t:+.2f}  beta={beta:+.2f}  n={nn}{sig}")
    print("\n  Read: a POSITIVE r on '3PM resid ~ raw arc_delta' means shooters beat their 3PM "
          "projection vs soft arc D (and miss vs tough) — the exploitable effect. |t|>2 = ship it.")


if __name__ == "__main__":
    main()
