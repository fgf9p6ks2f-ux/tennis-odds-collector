"""Breaking-news trigger layer — beat the structured feeds to star injury news, for free.

Sources (both verified reachable from the VM, no auth, no keys):
  1. RotoWire's WNBA news wire (rotowire.com/wnba/news.php) — the UPSTREAM of ESPN's own
     player blurbs ("Data Provided by Rotowire"), so reading it directly skips ESPN's
     surfacing lag. Structured: player link + headline + item id.
  2. Bluesky post search (api.bsky.app, unauthenticated) — where the WNBA beat migrated.
     Two standing queries + one targeted query per uncertain star on today's slate.

What a fresh hit does:
  - ntfy push with the actual quote/headline (urgent when an OUT-class phrase hits a
    rostered player on today's slate; high otherwise) — you read the news, you decide
    (overrides stay manual: the machine scouts, the human confirms).
  - touches /tmp/.force_fullscan so the next loop tick re-scans feeds + lines immediately.
  - appends to wnba_news_log.jsonl — the latency journal (news_seen vs official-report
    mark) that will PROVE/disprove this layer's speed edge at the checkpoint.

Self-throttled to >=55s between polls regardless of loop cadence. Dedupe via
wnba_news_seen.txt (RW item ids + bsky post uris, pruned to 3000).

    NTFY_TOPIC=xxx python3 wnba_news_watch.py     # one poll
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SEEN = HERE / "wnba_news_seen.txt"
LOG = HERE / "wnba_news_log.jsonl"
THROTTLE = HERE / ".news_watch_last"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) pickz-news/1.0"}

OUT_RX = re.compile(r"ruled out|out for (?:the |tonight|sunday|monday|tuesday|wednesday|thursday|"
                    r"friday|saturday)|will not play|won'?t play|has been ruled|sidelined|"
                    r"out indefinitely|miss(?:es)? (?:the rest|tonight|sunday|monday|tuesday|"
                    r"wednesday|thursday|friday|saturday)", re.I)
IN_RX = re.compile(r"will play|available|active(?! roster)|upgraded to (?:probable|available)|"
                   r"good to go|cleared to play|starting tonight|in the starting", re.I)
Q_RX = re.compile(r"questionable|game-?time decision|gtd\b|doubtful|day-?to-?day", re.I)


def _roster():
    try:
        pl = json.loads((HERE / "wnba_players_cache.json").read_text()).get("players", {})
        return {n: v.get("team") for n, v in pl.items()}
    except (OSError, ValueError):
        return {}


def _classify(text):
    if OUT_RX.search(text):
        return "OUT"
    if IN_RX.search(text):
        return "IN"
    if Q_RX.search(text):
        return "Q"
    return None


def _match_players(text, roster):
    hits = []
    low = text.lower()
    for full in roster:
        last = full.split()[-1].lower()
        if len(last) >= 4 and last in low:
            first = full.split()[0].lower()
            if first in low or f"{first[0]}. {last}" in low or f"{first[0]}.{last}" in low:
                hits.append(full)
            elif len(last) >= 6 and low.count(last):
                hits.append(full)                       # distinctive lastname alone (Ionescu)
    return list(dict.fromkeys(hits))


def rw_news():
    """[(id, player, headline)] newest-first from RotoWire's WNBA wire."""
    try:
        h = requests.get("https://www.rotowire.com/wnba/news.php", headers=UA, timeout=15).text
    except requests.RequestException:
        return []
    out = []
    blocks = re.findall(
        r'news-update__player-link" href="[^"]*?-(\d+)">([^<]+)</a>.*?news-update__headline[^>]*>([^<]+)',
        h, re.S)
    for pid, player, headline in blocks[:25]:
        out.append((f"rw|{pid}|{headline[:40]}", player.strip(), headline.strip()))
    return out


def bsky(queries):
    """[(uri, handle, text)] latest posts for each query."""
    out = []
    for q in queries:
        try:
            r = requests.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                             params={"q": q, "limit": 8, "sort": "latest"},
                             headers=UA, timeout=10)
            for p in (r.json().get("posts") or []):
                rec = p.get("record") or {}
                created = (rec.get("createdAt") or "")[:19]
                try:                                    # only the last 45 min — search returns history
                    age = (dt.datetime.utcnow()
                           - dt.datetime.fromisoformat(created)).total_seconds() / 60
                except ValueError:
                    continue
                if age > 45:
                    continue
                out.append((p.get("uri"), p.get("author", {}).get("handle", "?"),
                            (rec.get("text") or "").replace("\n", " ")))
        except (requests.RequestException, ValueError):
            continue
    return out


def uncertain_stars():
    """Today's slate's Q/Doubtful impact names — the targets for focused bsky queries."""
    try:
        import wnba_tonight as T
        import wnba_wowy as W
        pl = W.players()
        playing = set(T.tonight_matchups())
        inj = T.injuries()
        return [n for n, s in inj.items()
                if s in ("Questionable", "Doubtful") and pl.get(n)
                and pl[n]["team"] in playing and (pl[n]["min"] >= 20 or pl[n]["pts"] >= 10)][:2]
    except Exception:
        return []


def main():
    now = time.time()
    try:
        if now - float(THROTTLE.read_text()) < 55:
            return
    except (OSError, ValueError):
        pass
    THROTTLE.write_text(str(now))

    roster = _roster()
    cold_start = not SEEN.exists()          # first run: baseline the wire silently (no pings
    seen = set(SEEN.read_text().splitlines()) if SEEN.exists() else set()   # for stale items)
    topic = os.environ.get("NTFY_TOPIC")
    today_names = set()
    try:
        import wnba_tonight as T
        import wnba_wowy as W
        pl = W.players()
        playing = set(T.tonight_matchups())
        today_names = {n for n, v in pl.items() if v.get("team") in playing}
    except Exception:
        pass

    fresh = []
    for iid, player, headline in rw_news():
        if iid in seen:
            continue
        seen.add(iid)
        cls = _classify(headline)
        if cls and player in roster:
            fresh.append(("RW", player, headline, cls))
    stars = uncertain_stars()
    queries = ['wnba "ruled out"', 'wnba "will not play"'] + [f'"{n}"' for n in stars]
    for uri, handle, text in bsky(queries):
        if uri in seen:
            continue
        seen.add(uri)
        cls = _classify(text)
        players = _match_players(text, roster)
        if cls and players:
            fresh.append((f"@{handle}", players[0], text[:180], cls))

    if cold_start:
        SEEN.write_text("\n".join(sorted(seen)[-3000:]))
        print(f"news watch cold start — baselined {len(seen)} items, no pings")
        return
    trigger = False
    for src, player, text, cls in fresh:
        onslate = player in today_names
        LOG.open("a").write(json.dumps({
            "ts": dt.datetime.utcnow().isoformat()[:19], "src": src, "player": player,
            "class": cls, "onslate": onslate, "text": text[:200]}) + "\n")
        print(f"NEWS {cls} [{src}] {player}: {text[:90]}")
        if cls in ("OUT", "Q") and onslate:
            trigger = True
        if topic and (onslate or cls == "OUT"):
            prio = "urgent" if (cls == "OUT" and onslate) else "high"
            try:
                requests.post(f"https://ntfy.sh/{topic}",
                              data=f"{player} — {text[:190]}".encode(),
                              params={"title": f"📰 {src}", "priority": prio}, timeout=10)
            except requests.RequestException:
                pass
    if trigger:
        Path("/tmp/.force_fullscan").touch()
        print("news trigger -> forced fullscan")
    SEEN.write_text("\n".join(sorted(seen)[-3000:]))


if __name__ == "__main__":
    main()
