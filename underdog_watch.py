"""Underdog WNBA tweet watcher — read @Underdog__WNBA directly to close the ~1-2 min gap on injury
news. X killed free no-auth timeline reading (2026), so this uses YOUR X session cookie (auth_token
+ ct0) from env — you paste them once; this never sees your password. Public web Bearer + your
cookie -> GraphQL UserTweets. An injury-phrase hit -> ntfy + force fullscan (same hook as
wnba_news_watch). Kept LIGHT (plain requests, ~60s throttle) so it can't swap-thrash the loop box.

RELIABILITY: X rotates its GraphQL queryIds every few weeks — the #1 break risk — so we DISCOVER
them from X's own JS bundle (cached daily) instead of hardcoding, and FAIL LOUD (a once/day ntfy)
on an expired cookie or a changed endpoint, never silently. Silent no-op if X_AUTH_TOKEN is unset.

SETUP (once; use a BURNER X account, not your main — automated reads can get an account limited):
  x.com logged in -> DevTools > Application > Cookies > https://x.com -> copy the VALUES of
  `auth_token` and `ct0` into ~/wnba-loop.env:
      export X_AUTH_TOKEN=...
      export X_CT0=...
  Then: python3 underdog_watch.py --test   # verifies cookie + prints latest tweets

    python3 underdog_watch.py            # one poll (throttled); ntfy on a fresh injury hit
    python3 underdog_watch.py --test     # resolve handle + dump recent tweets (setup check)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
HANDLE = os.environ.get("X_HANDLE", "UnderdogWNBA")   # verified real handle (id 1577742107929968640)
SEEN = HERE / "underdog_seen.txt"
LOG = HERE / "underdog_log.jsonl"
QCACHE = HERE / ".x_query_ids.json"
UIDC = HERE / ".x_uid_cache.json"
THROTTLE = HERE / ".underdog_last"
PINGED = HERE / ".x_alert_pinged"
MIN_GAP = 60                       # >= seconds between polls — light on the 956MB box
FORCE = Path("/tmp/.force_fullscan")
BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
          "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")   # X's public web bearer (stable for years)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"}

# injury phrasing — reuse wnba_news_watch's regex so a hit means the same thing everywhere
try:
    from wnba_news_watch import OUT_RX, IN_RX
except Exception:
    OUT_RX = re.compile(r"ruled out|will not play|won'?t play|out (?:for|tonight)|sidelined|inactive",
                        re.I)
    IN_RX = re.compile(r"will play|active|cleared to play|good to go|upgraded", re.I)


def _cookie():
    at, ct0 = os.environ.get("X_AUTH_TOKEN"), os.environ.get("X_CT0")
    return (at, ct0) if at and ct0 else (None, None)


def _headers(ct0):
    return {**UA, "authorization": BEARER, "x-csrf-token": ct0,
            "cookie": f"auth_token={os.environ['X_AUTH_TOKEN']}; ct0={ct0}",
            "content-type": "application/json", "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session", "x-twitter-client-language": "en"}


def _alert(topic, body):
    """Fail-loud, deduped to once/day so a dead cookie pings you but doesn't spam."""
    day = dt.date.today().isoformat()
    try:
        if PINGED.exists() and PINGED.read_text().strip() == day:
            return
    except OSError:
        pass
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode(),
                          headers={"Title": "Pickz X-watch", "Priority": "high"}, timeout=10)
        except requests.RequestException:
            pass
    try:
        PINGED.write_text(day)
    except OSError:
        pass
    print("ALERT:", body)


def _query_ids(ct0=None):
    """{op: queryId} discovered from X's JS bundle (cached daily) so a queryId rotation self-heals.
    The bundle URLs only appear in the AUTHENTICATED app shell, so we fetch x.com with the cookie.
    Falls back to the cached copy, then to last-known-good hardcoded ids, if discovery fails."""
    today = dt.date.today().isoformat()
    try:
        c = json.loads(QCACHE.read_text())
        if c.get("date") == today and "UserTweets" in c.get("ids", {}):
            return c["ids"]
    except (ValueError, OSError):
        c = {}
    ids = {}
    hh = {**UA}
    if ct0 and os.environ.get("X_AUTH_TOKEN"):
        hh["cookie"] = f"auth_token={os.environ['X_AUTH_TOKEN']}; ct0={ct0}"
    try:
        home = requests.get("https://x.com/", headers=hh, timeout=15).text
        js = re.findall(r"https://abs\.twimg\.com/responsive-web/client-web[^\"']*?main[^\"']*?\.js", home)
        # the loader lists many chunk files; the query map lives in the api chunk — scan a few
        cand = re.findall(r"https://abs\.twimg\.com/responsive-web/client-web[^\"']*?\.js", home)
        for url in ([*js, *cand])[:8]:
            try:
                body = requests.get(url, headers=UA, timeout=15).text
            except requests.RequestException:
                continue
            for op in ("UserByScreenName", "UserTweets"):
                m = re.search(r'queryId:"([^"]+)",operationName:"%s"' % op, body) or \
                    re.search(r'operationName:"%s",queryId:"([^"]+)"' % op, body)
                if m:
                    ids[op] = m.group(1)
            if "UserByScreenName" in ids and "UserTweets" in ids:
                break
    except requests.RequestException:
        pass
    if "UserTweets" in ids and "UserByScreenName" in ids:
        try:
            QCACHE.write_text(json.dumps({"date": today, "ids": ids}))
        except OSError:
            pass
        return ids
    # discovery failed -> last-known-good cache, else hardcoded (may be stale -> fail-loud on 404)
    return c.get("ids") or {"UserByScreenName": "32pL5BWe9WKeSK1MoPvFQQ",
                            "UserTweets": "E3opETHurmVJflFsUBVuUQ"}


def _uid(ids, ct0):
    try:
        c = json.loads(UIDC.read_text())
        if c.get("handle") == HANDLE and c.get("uid"):
            return c["uid"]
    except (ValueError, OSError):
        pass
    if "UserByScreenName" not in ids:
        return None
    var = {"screen_name": HANDLE}
    feat = {"hidden_profile_subscriptions_enabled": True, "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False, "subscriptions_verification_info_is_identity_verified_enabled": True,
            "subscriptions_verification_info_verified_since_enabled": True, "highlights_tweets_tab_ui_enabled": True,
            "responsive_web_twitter_article_notes_tab_enabled": True, "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True}
    url = (f"https://x.com/i/api/graphql/{ids['UserByScreenName']}/UserByScreenName"
           f"?variables={requests.utils.quote(json.dumps(var))}&features={requests.utils.quote(json.dumps(feat))}")
    r = requests.get(url, headers=_headers(ct0), timeout=15)
    if r.status_code in (401, 403):
        return "AUTH"
    try:
        uid = r.json()["data"]["user"]["result"]["rest_id"]
        UIDC.write_text(json.dumps({"handle": HANDLE, "uid": uid}))
        return uid
    except (ValueError, KeyError, TypeError):
        return None


def _tweets(ids, ct0, uid):
    var = {"userId": uid, "count": 20, "includePromotedContent": False, "withQuickPromoteEligibilityTweetFields": False,
           "withVoice": False, "withV2Timeline": True}
    feat = {"responsive_web_graphql_exclude_directive_enabled": True, "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True, "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False, "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "tweetypie_unmention_optimization_enabled": True, "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True, "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True, "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False, "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True, "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "rweb_video_timestamps_enabled": True, "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True, "responsive_web_enhance_cards_enabled": False}
    url = (f"https://x.com/i/api/graphql/{ids['UserTweets']}/UserTweets"
           f"?variables={requests.utils.quote(json.dumps(var))}&features={requests.utils.quote(json.dumps(feat))}")
    r = requests.get(url, headers=_headers(ct0), timeout=15)
    if r.status_code in (401, 403):
        return "AUTH"
    if r.status_code == 404:
        return "ENDPOINT"
    if r.status_code == 429:
        return "RATE"                          # rate-limited (transient) — back off, do NOT alert
    try:
        d = r.json()
    except ValueError:
        # a rate-limit / interstitial can return non-JSON ("Rate limit exceeded") — treat as transient
        return "RATE" if "rate limit" in r.text.lower() else "PARSE"
    if d.get("errors") and not d.get("data"):
        return "ENDPOINT"
    # Deep-walk for tweet objects (rest_id + legacy.full_text) instead of a fixed path — X renames the
    # timeline container often (timeline_v2 -> timeline ...), so walking the whole payload is what keeps
    # this from breaking on every X redesign. Dedup by id; order doesn't matter (we filter vs `seen`).
    tw = {}

    def _walk(o):
        if isinstance(o, dict):
            lg = o.get("legacy")
            if isinstance(lg, dict) and lg.get("full_text") and o.get("rest_id"):
                tw[str(o["rest_id"])] = lg["full_text"]
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)
    _walk(d.get("data") or {})
    return list(tw.items()) if tw else "PARSE"


def _seen():
    try:
        return set(SEEN.read_text().split())
    except OSError:
        return set()


def run(test=False, force=False):
    topic = os.environ.get("NTFY_TOPIC")
    at, ct0 = _cookie()
    if not at:
        print("underdog_watch: no X_AUTH_TOKEN -> benched")
        return
    if not test and not force:                 # force=True in --loop mode (the loop's own sleep paces it)
        try:
            if THROTTLE.exists() and time.time() - THROTTLE.stat().st_mtime < MIN_GAP:
                return
        except OSError:
            pass
        THROTTLE.touch()
    ids = _query_ids(ct0)
    if "UserTweets" not in ids or "UserByScreenName" not in ids:
        _alert(topic, "X-watch: couldn't discover X GraphQL endpoints (bundle changed) — needs a look")
        return
    uid = _uid(ids, ct0)
    if uid == "AUTH":
        _alert(topic, "X-watch: your X cookie is expired/invalid — refresh X_AUTH_TOKEN + X_CT0 in wnba-loop.env")
        return
    if not uid:
        _alert(topic, f"X-watch: couldn't resolve @{HANDLE} (endpoint/format change) — needs a look")
        return
    tw = _tweets(ids, ct0, uid)
    if tw == "RATE":                           # X rate limit hit — normal at a fast cadence; skip quietly
        print("underdog_watch: rate-limited (429) — backing off this cycle")
        return
    if tw in ("AUTH", "ENDPOINT", "PARSE"):
        _alert(topic, f"X-watch: UserTweets failed ({tw}) — cookie or X endpoint changed, needs a look")
        return
    if test:
        print(f"@{HANDLE} uid={uid} — {len(tw)} tweets:")
        for tid, txt in tw[:8]:
            print(f"  [{tid}] {txt[:140].replace(chr(10),' ')}")
        return
    seen = _seen()
    first = not SEEN.exists()            # first-ever run = baseline: mark the backlog seen, don't alert
    fresh, hits = [], 0
    for tid, txt in tw:
        if tid in seen:
            continue
        fresh.append(tid)
        if first:
            continue                     # no push storm on the historical timeline
        if OUT_RX.search(txt) or IN_RX.search(txt):
            hits += 1
            FORCE.touch()                              # kick the loop to re-scan lines NOW
            if topic:
                pri = "urgent" if OUT_RX.search(txt) else "high"
                try:
                    requests.post(f"https://ntfy.sh/{topic}",
                                  data=f"\U0001f6a8 @{HANDLE}: {txt[:180]}".encode(),
                                  headers={"Title": "Underdog WNBA", "Priority": pri}, timeout=10)
                except requests.RequestException:
                    pass
            try:
                with LOG.open("a") as f:
                    f.write(json.dumps({"t": dt.datetime.now(dt.timezone.utc).isoformat(),
                                        "id": tid, "text": txt[:280]}) + "\n")
            except OSError:
                pass
            print(f"NEWS trigger @{HANDLE}: {txt[:120]}")
    if fresh:
        try:
            SEEN.write_text(" ".join((list(seen) + fresh)[-3000:]))
        except OSError:
            pass
    print(f"underdog_watch: {len(tw)} tweets, {len(fresh)} new, {hits} injury hits")


def loop(interval):
    """Persistent daemon: poll every `interval` seconds in ONE process (no per-poll process spawn),
    so it's light on the box even at 10s. Errors are swallowed so a transient X hiccup can't kill it."""
    print(f"underdog_watch: daemon polling @{HANDLE} every {interval}s")
    while True:
        try:
            run(force=True)
        except Exception as e:
            print("underdog_watch loop error:", str(e)[:120])
        time.sleep(interval)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        secs = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 10
        loop(max(5, secs))
    else:
        run(test="--test" in sys.argv)
