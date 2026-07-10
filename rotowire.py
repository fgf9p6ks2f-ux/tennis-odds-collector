"""RotoWire WNBA lineups + injuries — free, server-rendered, datacenter-reachable.

Gives what the ESPN feed doesn't: CONFIRMED vs PROJECTED starting lineups (authoritative
starter confirmation, posted hours ahead and locked ~30-60 min pre-tip) plus inline injury
tags (OUT/GTD/…). It's structured HTML, so there's NO free-text parsing — far more reliable
than scraping tweets, and unlike X / the betting books it doesn't block cloud IPs.

    python rotowire.py            # print tonight's lineups + injuries

Consumers: starter_status(team, player) -> 'confirmed'|'projected'|None for a starter, and
out_players() -> {normalized_name: status} to cross-check / beat the ESPN injury feed.
"""
from __future__ import annotations

import html as H
import re
import unicodedata

URL = "https://www.rotowire.com/wnba/lineups.php"

# RotoWire abbrev -> the abbrev our ESPN layer uses (WNBA)
TEAM_FIX = {"PHO": "PHX", "LVA": "LV", "GSV": "GS", "NYL": "NY", "CON": "CONN",
            "WAS": "WSH", "LAS": "LA"}


def _sess():
    from curl_cffi import requests as cr        # lazy: helpers import w/o curl_cffi
    return cr.Session(impersonate="chrome")


def fetch():
    return _sess().get(URL, timeout=25).text


def norm(name: str) -> str:
    """Fold to 'first-initial + lastname' so RotoWire's 'M. Billings' matches ESPN's
    'Monique Billings'. Accent-fold; keep the last token + leading initial."""
    s = unicodedata.normalize("NFKD", H.unescape(name or "")).encode("ascii", "ignore").decode()
    toks = s.replace(".", " ").lower().split()
    if len(toks) < 2:
        return " ".join(toks)
    return f"{toks[0][0]} {toks[-1]}"           # 'm billings', 'v ayayi'


def parse(txt):
    """-> list of team dicts {team, status, starters:[(pos,name,inj)], out:[name]}.
    Abbrevs and lineup lists both appear in document order (visit, home, visit, …), so we
    zip them; each list carries its own confirmed/expected status + player rows."""
    abbrs = re.findall(r'lineup__abbr[^>]*>([A-Z]{2,4})<', txt)
    lists = re.findall(r'<ul class="lineup__list[^"]*">(.*?)</ul>', txt, re.S)
    teams = []
    for abbr, blk in zip(abbrs, lists):
        st = re.search(r'lineup__status\s+is-([a-z]+)"', blk)
        status = {"confirmed": "confirmed", "expected": "projected"}.get(
            st.group(1) if st else "", "projected")
        starters, out = [], []
        for pos, nm, inj in re.findall(
                r'lineup__pos">([A-Z]{1,3})</div>\s*<a[^>]*>([^<]+)</a>'
                r'(?:\s*<span class="lineup__inj[^"]*">([^<]*)</span>)?', blk):
            nm = H.unescape(nm.strip())
            inj = (inj or "").strip().upper()
            starters.append((pos, nm, inj))
            if inj in ("OUT", "GTD", "DOUBTFUL"):
                out.append(nm)
        if starters:
            teams.append({"team": TEAM_FIX.get(abbr, abbr), "status": status,
                          "starters": starters, "out": out})
    return teams


def board():
    return parse(fetch())


def starter_status(teams, team, player):
    """'confirmed' | 'projected' | None — is `player` in `team`'s starting five, and is that
    five confirmed or still projected? Name-matched on first-initial + lastname."""
    key = norm(player)
    for t in teams:
        if t["team"] != team.upper():
            continue
        if any(norm(nm) == key for _, nm, _ in t["starters"]):
            return t["status"]
    return None


def out_players(teams):
    """{normalized_name: 'OUT'} across all teams — RotoWire's ruled-out list, to cross-check
    (and often beat) the ESPN injury feed."""
    return {norm(nm): "OUT" for t in teams for nm in t["out"]}


if __name__ == "__main__":
    tt = board()
    print(f"RotoWire WNBA — {len(tt)} team lineups\n")
    for t in tt:
        tag = "✓ CONFIRMED" if t["status"] == "confirmed" else "· projected"
        print(f"[{tag}] {t['team']}: " + ", ".join(
            f"{nm}{' ('+inj+')' if inj else ''}" for _, nm, inj in t["starters"]))
    outs = out_players(tt)
    if outs:
        print("\nruled OUT:", ", ".join(sorted(outs)))
