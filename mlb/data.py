"""MLB Stats API client (statsapi.mlb.com — free, no key).

Just the pieces the strikeout model needs: team K%, starter game logs, and today's
probable pitchers.
"""
from __future__ import annotations

import time

import requests

API = "https://statsapi.mlb.com/api/v1"


def _get(path: str, **params):
    for attempt in range(3):
        try:
            r = requests.get(f"{API}{path}", params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"MLB API failed: {path} {params}")


def team_kpct(season: int) -> tuple[dict[int, float], float]:
    """Return ({team_id: K% per PA}, league_K%) for the season."""
    d = _get("/teams/stats", stats="season", group="hitting", season=season, sportId=1)
    out, tot_k, tot_pa = {}, 0, 0
    for t in d["stats"][0]["splits"]:
        st = t["stat"]
        pa = st.get("plateAppearances") or 0
        k = st.get("strikeOuts") or 0
        if pa:
            out[t["team"]["id"]] = k / pa
            tot_k += k
            tot_pa += pa
    return out, (tot_k / tot_pa if tot_pa else 0.22)


def team_kpct_by_hand(season: int) -> tuple[dict[int, dict], dict]:
    """({team_id: {'L': K% vs LHP, 'R': K% vs RHP}}, {'L':lg, 'R':lg}). A pitcher who
    throws hand H faces the opponent's split vs H."""
    from collections import defaultdict
    out: dict = defaultdict(dict)
    lg = {"L": [0, 0], "R": [0, 0]}
    for hand, sit in (("L", "vl"), ("R", "vr")):
        d = _get("/teams/stats", stats="statSplits", group="hitting", season=season,
                 sportId=1, sitCodes=sit)
        for t in d["stats"][0]["splits"]:
            st = t["stat"]
            pa = st.get("plateAppearances") or 0
            k = st.get("strikeOuts") or 0
            if pa:
                out[t["team"]["id"]][hand] = k / pa
                lg[hand][0] += k
                lg[hand][1] += pa
    lg_hand = {h: (lg[h][0] / lg[h][1] if lg[h][1] else 0.22) for h in "LR"}
    return dict(out), lg_hand


def savant_whiff(season: int) -> dict[int, tuple[float, float]]:
    """Baseball Savant: {player_id: (season K% frac, whiff% frac)} for qualified
    pitchers. Whiff% is the underlying swing-and-miss skill behind strikeouts."""
    import csv
    import io
    url = (f"https://baseballsavant.mlb.com/leaderboard/custom?year={season}"
           f"&type=pitcher&filter=&min=q&selections=pa,k_percent,whiff_percent&csv=true")
    # decode utf-8-sig to strip the BOM, else the quoted "Last, First" column
    # misaligns every field by one.
    txt = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30
                       ).content.decode("utf-8-sig")
    out = {}
    for row in csv.DictReader(io.StringIO(txt)):
        try:
            out[int(row["player_id"])] = (float(row["k_percent"]) / 100,
                                          float(row["whiff_percent"]) / 100)
        except (ValueError, KeyError, TypeError):
            pass
    return out


def batter_kpct_by_hand(season: int) -> tuple[dict[int, dict], dict]:
    """({batter_id: {'L': K% vs LHP, 'R': K% vs RHP}}, {'L':lg,'R':lg}) for PA-weighting
    a real lineup against the starter's hand."""
    from collections import defaultdict
    out: dict = defaultdict(dict)
    lg = {"L": [0, 0], "R": [0, 0]}
    for hand, sit in (("L", "vl"), ("R", "vr")):
        d = _get("/stats", stats="statSplits", group="hitting", season=season,
                 sportId=1, sitCodes=sit, limit=3000, playerPool="all")
        for b in d["stats"][0]["splits"]:
            st = b["stat"]
            pa = st.get("plateAppearances") or 0
            pid = (b.get("player") or {}).get("id")
            if pa >= 20 and pid:
                out[pid][hand] = (st.get("strikeOuts") or 0) / pa
                lg[hand][0] += st.get("strikeOuts") or 0
                lg[hand][1] += pa
    lg_hand = {h: (lg[h][0] / lg[h][1] if lg[h][1] else 0.22) for h in "LR"}
    return dict(out), lg_hand


def game_lineup(game_pk: int) -> dict:
    """{'home': [batter_ids in order], 'away': [...]} from the boxscore."""
    d = _get(f"/game/{game_pk}/boxscore")
    out = {}
    for side in ("home", "away"):
        out[side] = (d.get("teams", {}).get(side, {}).get("battingOrder") or [])[:9]
    return out


def pitcher_hand(pid: int) -> str:
    d = _get(f"/people/{pid}")
    return (d["people"][0].get("pitchHand") or {}).get("code", "R")


def team_obp(season: int) -> tuple[dict[int, float], float]:
    """({team_id: on-base%}, league OBP) — the opponent-labor signal for outs props."""
    d = _get("/teams/stats", stats="season", group="hitting", season=season, sportId=1)
    out, tot = {}, []
    for t in d["stats"][0]["splits"]:
        obp = t["stat"].get("obp")
        if obp:
            v = float(obp)
            out[t["team"]["id"]] = v
            tot.append(v)
    return out, (sum(tot) / len(tot) if tot else 0.315)


def starter_ids(season: int, limit: int = 100) -> list[int]:
    """Pitcher ids with the most games started that season."""
    d = _get("/stats", stats="season", group="pitching", sportId=1, season=season,
             limit=400, gameType="R")
    rows = []
    for s in d["stats"][0]["splits"]:
        gs = s["stat"].get("gamesStarted") or 0
        pid = s.get("player", {}).get("id")
        if pid and gs >= 10:
            rows.append((gs, pid))
    rows.sort(reverse=True)
    return [pid for _gs, pid in rows[:limit]]


def pitcher_season(pid: int, season: int) -> dict | None:
    """Season-to-date {k, bf, gs} for a pitcher (for live projection)."""
    d = _get(f"/people/{pid}/stats", stats="season", group="pitching", season=season)
    sp = (d.get("stats") or [{}])[0].get("splits") or []
    if not sp:
        return None
    st = sp[0]["stat"]
    return {"k": st.get("strikeOuts") or 0, "bf": st.get("battersFaced") or 0,
            "gs": st.get("gamesStarted") or 0}


def pitcher_gamelog(pid: int, season: int) -> list[dict]:
    """Chronological list of that pitcher's STARTS: date, opp_id, K, BF, outs."""
    d = _get(f"/people/{pid}/stats", stats="gameLog", group="pitching", season=season)
    st = d.get("stats") or []
    if not st:
        return []
    out = []
    for g in st[0]["splits"]:
        s = g["stat"]
        if not (s.get("gamesStarted") or 0):
            continue
        out.append({"date": g.get("date"), "opp_id": g.get("opponent", {}).get("id"),
                    "k": s.get("strikeOuts") or 0, "bf": s.get("battersFaced") or 0,
                    "outs": s.get("outs") or 0,
                    "pitches": s.get("numberOfPitches") or 0,
                    "game_pk": (g.get("game") or {}).get("gamePk"),
                    "is_home": g.get("isHome")})
    return out


def find_pitcher(name: str) -> int | None:
    """Resolve a pitcher name (from collected props) to an MLB player id."""
    try:
        d = _get("/people/search", names=name)
        people = d.get("people") or []
        return people[0]["id"] if people else None
    except Exception:
        return None


find_player = find_pitcher   # people/search resolves batters just the same


# ---- batter (total-bases) model inputs -------------------------------------

def _hit_line(st: dict) -> dict:
    """Normalize a hitting stat blob to the fields the TB model needs."""
    return {"pa": st.get("plateAppearances") or 0, "ab": st.get("atBats") or 0,
            "h": st.get("hits") or 0, "2b": st.get("doubles") or 0,
            "3b": st.get("triples") or 0, "hr": st.get("homeRuns") or 0,
            "tb": st.get("totalBases") or 0, "gp": st.get("gamesPlayed") or 0}


def batter_season(pid: int, season: int) -> dict | None:
    """Season-to-date hitting line for one batter (live projection)."""
    d = _get(f"/people/{pid}/stats", stats="season", group="hitting", season=season)
    sp = (d.get("stats") or [{}])[0].get("splits") or []
    return _hit_line(sp[0]["stat"]) if sp else None


def batter_gamelog(pid: int, season: int) -> list[dict]:
    """Chronological game-by-game hitting lines (for the walk-forward backtest)."""
    d = _get(f"/people/{pid}/stats", stats="gameLog", group="hitting", season=season)
    st = d.get("stats") or []
    if not st:
        return []
    out = []
    for g in st[0]["splits"]:
        line = _hit_line(g["stat"])
        line.update(date=g.get("date"), opp_id=(g.get("opponent") or {}).get("id"),
                    game_pk=(g.get("game") or {}).get("gamePk"))
        out.append(line)
    return out


def all_batter_lines(season: int, min_pa: int = 50) -> dict[int, dict]:
    """{batter_id: season hitting line} in bulk (backtest priors + live lineups)."""
    d = _get("/stats", stats="season", group="hitting", sportId=1, season=season,
             limit=3000, playerPool="all", gameType="R")
    out = {}
    for s in d["stats"][0]["splits"]:
        pid = (s.get("player") or {}).get("id")
        line = _hit_line(s["stat"])
        line["name"] = (s.get("player") or {}).get("fullName")
        if pid and line["pa"] >= min_pa:
            out[pid] = line
    return out


def batter_hand_factor(season: int) -> tuple[dict[int, dict], dict]:
    """({batter_id: {'L': TB/PA vs LHP, 'R': TB/PA vs RHP}}, {'L':lg,'R':lg}). The platoon
    signal — a batter faces the starter's hand."""
    from collections import defaultdict
    out: dict = defaultdict(dict)
    lg = {"L": [0, 0], "R": [0, 0]}
    for hand, sit in (("L", "vl"), ("R", "vr")):
        d = _get("/stats", stats="statSplits", group="hitting", season=season,
                 sportId=1, sitCodes=sit, limit=3000, playerPool="all")
        for b in d["stats"][0]["splits"]:
            st, pid = b["stat"], (b.get("player") or {}).get("id")
            pa, tb = st.get("plateAppearances") or 0, st.get("totalBases") or 0
            if pid and pa >= 20:
                out[pid][hand] = tb / pa
                lg[hand][0] += tb
                lg[hand][1] += pa
    lg_hand = {h: (lg[h][0] / lg[h][1] if lg[h][1] else 0.42) for h in "LR"}
    return dict(out), lg_hand


def team_pitching_tb_factor(season: int) -> tuple[dict[int, float], float]:
    """({team_id: TB-allowed-per-BF}, league avg). Opponent-staff suppression for the
    batter matchup. TB = H + 2B + 2*3B + 3*HR."""
    d = _get("/teams/stats", stats="season", group="pitching", season=season, sportId=1)
    out, tot_tb, tot_bf = {}, 0, 0
    for t in d["stats"][0]["splits"]:
        st = t["stat"]
        bf = st.get("battersFaced") or 0
        tb = ((st.get("hits") or 0) + (st.get("doubles") or 0)
              + 2 * (st.get("triples") or 0) + 3 * (st.get("homeRuns") or 0))
        if bf:
            out[t["team"]["id"]] = tb / bf
            tot_tb += tb
            tot_bf += bf
    return out, (tot_tb / tot_bf if tot_bf else 0.44)


def probables(date: str) -> list[dict]:
    """Today's games with each side's probable pitcher id + opponent team id."""
    d = _get("/schedule", sportId=1, hydrate="probablePitcher", date=date)
    games = []
    for day in d.get("dates", []):
        for g in day["games"]:
            h, a = g["teams"]["home"], g["teams"]["away"]
            for side, opp in ((h, a), (a, h)):
                pp = side.get("probablePitcher")
                if pp:
                    games.append({"pitcher_id": pp["id"], "pitcher": pp["fullName"],
                                  "team_id": side["team"]["id"], "opp_id": opp["team"]["id"],
                                  "opp": opp["team"]["name"], "game_date": g.get("gameDate")})
    return games
