"""OFFICIAL WNBA injury report — the #1 confirmation source (user-sourced 2026-07-18).

League-published, game-dated PDF at ak-static.cms.nba.com/referee/wnba_injury/
Injury-Report_YYYY-MM-DD_HH_MMAM.pdf, refreshed on the :00/:15/:30/:45 marks. Gives the
three things no aggregator does: rulings bound to a SPECIFIC game (today AND tomorrow),
an explicit 'NOT YET SUBMITTED' state (no ruling exists — the Boston 7/18 case), and
league authority. Cached per 15-min mark, so the 75s loop fetches at most 4x/hour.

    report()  -> {"fetched": iso, "stamp": url_stamp,
                  "rows": [{game_date, matchup, team, player, status, reason}],
                  "submitted": {(game_date, team_abbr): bool}}
    confirmed_by_date() -> {date: {player: status}} for Out/Doubtful/Questionable/Probable
"""
from __future__ import annotations

import datetime as dt
import io
import json
import unicodedata
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CACHE = HERE / "wnba_injury_report_cache.json"
ET = dt.timezone(dt.timedelta(hours=-4))
BASE = "https://ak-static.cms.nba.com/referee/wnba_injury/Injury-Report_{d}_{h:02d}_{m:02d}{ap}.pdf"

TEAMS = {"New York Liberty": "NY", "Indiana Fever": "IND", "Portland Fire": "POR",
         "Minnesota Lynx": "MIN", "Golden State Valkyries": "GS", "Washington Mystics": "WSH",
         "Los Angeles Sparks": "LA", "Dallas Wings": "DAL", "Chicago Sky": "CHI",
         "Atlanta Dream": "ATL", "Connecticut Sun": "CON", "Phoenix Mercury": "PHX",
         "Seattle Storm": "SEA", "Las Vegas Aces": "LV", "Toronto Tempo": "TOR"}
STATUSES = {"Out", "Doubtful", "Questionable", "Probable", "Available"}


def _norm(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def _marks(now_et, back=10):
    """Candidate (date_str, hour12, minute, AM/PM) stamps, newest first, 15-min grid."""
    t = now_et.replace(minute=(now_et.minute // 15) * 15, second=0, microsecond=0)
    for _ in range(back):
        h12 = t.hour % 12 or 12
        yield t.strftime("%Y-%m-%d"), h12, t.minute, ("AM" if t.hour < 12 else "PM"), t
        t -= dt.timedelta(minutes=15)


def _parse(pdf_bytes):
    from pypdf import PdfReader
    toks = []
    for pg in PdfReader(io.BytesIO(pdf_bytes)).pages:
        toks += [w for w in (pg.extract_text() or "").split("\n") if w.strip()]
    rows, submitted = [], {}
    gdate = matchup = None
    team = None
    i = 0
    tnames = sorted(TEAMS, key=lambda x: -len(x.split()))
    while i < len(toks):
        tk = toks[i].strip()
        # game date
        if len(tk) == 10 and tk[2] == "/" and tk[5] == "/":
            try:
                gdate = dt.datetime.strptime(tk, "%m/%d/%Y").date().isoformat()
                i += 1
                continue
            except ValueError:
                pass
        # matchup code
        if "@" in tk and 5 <= len(tk) <= 8 and tk.replace("@", "").isalpha() and tk.isupper():
            matchup = tk
            i += 1
            continue
        # team full name (multi-token)
        hit = None
        for name in tnames:
            parts = name.split()
            if toks[i:i + len(parts)] == parts:
                hit = name
                i += len(parts)
                break
        if hit:
            team = TEAMS[hit]
            if gdate is not None:
                submitted.setdefault((gdate, team), True)
            continue
        # NOT YET SUBMITTED
        if tk == "NOT" and toks[i + 1:i + 3] == ["YET", "SUBMITTED"]:
            if gdate and team:
                submitted[(gdate, team)] = False
            i += 3
            continue
        # player row: surname token(s) ending with a comma
        if tk.endswith(",") and team and gdate:
            last = [tk[:-1]]
            j = i - 1                                   # multi-word surnames precede? tokens flow
            first = []
            k = i + 1
            while k < len(toks) and toks[k].strip() not in STATUSES:
                first.append(toks[k].strip())
                k += 1
                if len(first) > 4:                       # runaway guard
                    break
            if k < len(toks) and toks[k].strip() in STATUSES:
                status = toks[k].strip()
                # reason: tokens until the next structural marker
                r = []
                m2 = k + 1
                while m2 < len(toks):
                    nxt = toks[m2].strip()
                    if (nxt.endswith(",") or nxt in ("NOT",) or "@" in nxt
                            or (len(nxt) == 10 and nxt[2:3] == "/")
                            or any(toks[m2:m2 + len(nm.split())] == nm.split() for nm in tnames)):
                        break
                    r.append(nxt)
                    m2 += 1
                player = " ".join(first) + " " + " ".join(last)
                rows.append({"game_date": gdate, "matchup": matchup, "team": team,
                             "player": _norm(player), "status": status,
                             "reason": " ".join(r)[:80]})
                i = m2
                continue
        i += 1
    return rows, {f"{d}|{t}": v for (d, t), v in submitted.items()}


def report(max_age_min=14):
    """Latest parsed report, cached per 15-min mark."""
    now = dt.datetime.now(ET)
    try:
        c = json.loads(CACHE.read_text())
        age = (now - dt.datetime.fromisoformat(c["fetched"])).total_seconds() / 60
        cur_mark = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0).isoformat()
        if c.get("mark") == cur_mark or age < 3:
            return c
    except (OSError, ValueError, KeyError):
        pass
    for d, h12, m, ap, t in _marks(now):
        url = BASE.format(d=d, h=h12, m=m, ap=ap)
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                rows, submitted = _parse(r.content)
                out = {"fetched": now.isoformat(), "stamp": f"{d}_{h12:02d}_{m:02d}{ap}",
                       "mark": now.replace(minute=(now.minute // 15) * 15, second=0,
                                           microsecond=0).isoformat(),
                       "rows": rows, "submitted": submitted}
                CACHE.write_text(json.dumps(out))
                return out
        except requests.RequestException:
            continue
    return {"fetched": now.isoformat(), "stamp": None, "rows": [], "submitted": {}}


def confirmed_by_date():
    """{game_date: {player_name: status}} from the official report (all statuses)."""
    rep = report()
    out = {}
    for r in rep.get("rows", []):
        out.setdefault(r["game_date"], {})[r["player"]] = r["status"]
    return out


if __name__ == "__main__":
    rep = report()
    print("stamp:", rep.get("stamp"), "| rows:", len(rep.get("rows", [])))
    for r in rep.get("rows", []):
        print(f"  {r['game_date']} {r['matchup']:<9} {r['team']:<4} {r['player']:<24} "
              f"{r['status']:<12} {r['reason'][:40]}")
    print("submitted:", rep.get("submitted"))
