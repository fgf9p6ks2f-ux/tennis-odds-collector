"""Free multi-source WNBA injury-news aggregator — a faster front-end for the injury detector.

The ESPN injury *status* feed and RotoWire *lineups* lag the break (they update after a formal
designation / lineup post). RotoWire's PLAYER-NEWS feed, though, posts a blurb within minutes of a
beat reporter's tweet (that's their business) — so it catches "X will not play" earlier. Google
News RSS is a broad backup. This polls both, extracts genuine availability items (not box-score
recaps), matches them to our roster, and reports the NEW ones so the watch loop can fire a fast
heads-up + trigger a scan BEFORE ESPN/RotoWire status catches up.

Not X-speed (the fastest breaks are on walled X), but a real free upgrade over one feed.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path

try:
    from curl_cffi import requests as _R

    def _get(url):
        return _R.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, impersonate="chrome")
except Exception:                                            # pragma: no cover
    import requests as _R

    def _get(url):
        return _R.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)

HERE = Path(__file__).resolve().parent
SEEN = HERE / "wnba_news_seen.txt"
ROTO_NEWS = "https://www.rotowire.com/wnba/news.php"
GNEWS = "https://news.google.com/rss/search?q=WNBA%20(injury%20OR%20out%20OR%20questionable)%20when:1d&hl=en-US&gl=US&ceid=US:en"

# STRONG availability signal (a real status update, not a stat-line recap)
OUT_RE = re.compile(r"(?i)\b(ruled out|will not play|won'?t play|will miss|expected to miss|out (?:for|with|indefinitely|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Monday)|did not (?:play|dress)|inactive|sidelined|won'?t (?:suit up|return)|has been ruled|out of the lineup)\b")
GTD_RE = re.compile(r"(?i)\b(questionable|doubtful|game-time decision|gtd|listed as|probable|is a game-time)\b")


def rotowire_news():
    """[(player, blurb, status)] from RotoWire's WNBA player-news feed (the fast source)."""
    try:
        t = _get(ROTO_NEWS).text
    except Exception:
        return []
    out = []
    for m in re.finditer(r'news-update__player[^>]*>\s*<a[^>]*>([^<]+)</a>(.*?)'
                         r'news-update__news[^>]*>(.*?)</div>', t, re.S):
        player = html.unescape(m.group(1).strip())
        blurb = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        st = "out" if OUT_RE.search(blurb) else ("gtd" if GTD_RE.search(blurb) else None)
        if st:
            out.append((player, blurb[:200], st))
    return out


def google_news():
    """[(headline, link, status)] — broad WNBA injury headlines from Google News RSS (backup)."""
    try:
        t = _get(GNEWS).text
    except Exception:
        return []
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", t, re.S):
        block = m.group(1)
        title = html.unescape(re.sub(r"<[^>]+>", "", (re.search(r"<title>(.*?)</title>", block, re.S) or [None, ""])[1])).strip()
        st = "out" if OUT_RE.search(title) else ("gtd" if GTD_RE.search(title) else None)
        if st:
            out.append((title[:200], "", st))
    return out


def new_items(roster_names=None):
    """New (unseen) availability items across sources, matched to our roster when possible. Returns
    [{player, text, status, src}]; records what it's seen so each break fires once."""
    seen = set(SEEN.read_text().splitlines()) if SEEN.exists() else set()
    roster = {_norm(n): n for n in (roster_names or [])}
    fresh, add = [], []
    for player, blurb, st in rotowire_news():
        key = "rw|" + _norm(player) + "|" + blurb[:40]
        if key in seen:
            continue
        add.append(key)
        full = roster.get(_norm(player))
        fresh.append({"player": full or player, "text": blurb, "status": st, "src": "rotowire",
                      "on_roster": full is not None})
    for title, _link, st in google_news():
        key = "gn|" + title[:60]
        if key in seen:
            continue
        add.append(key)
        hit = next((roster[k] for k in roster if k in _norm(title)), None)
        fresh.append({"player": hit or "?", "text": title, "status": st, "src": "gnews",
                      "on_roster": hit is not None})
    if add:
        with SEEN.open("a") as f:
            f.write("\n".join(add) + "\n")
    return fresh


def references_today(text):
    """True if the blurb is about TODAY's game (so a 'Saturday's game' item doesn't fire on Friday).
    A news OUT is only merged into tonight's injuries when this holds; otherwise it's future info."""
    now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))  # ET
    wd = now.strftime("%A")
    return bool(re.search(r"(?i)\b(today|tonight|this evening|" + wd + r")\b", text or ""))


def _norm(name):
    return re.sub(r"[^a-z]", "", (name or "").lower())


if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv and SEEN.exists():
        SEEN.unlink()
    items = new_items()
    print(f"{len(items)} new availability items:")
    for it in sorted(items, key=lambda x: not x["on_roster"]):
        tag = "✓roster" if it["on_roster"] else "?"
        print(f"  [{it['status'].upper()}] {it['player'][:22]:22} {tag} · {it['src']} · {it['text'][:80]}")
