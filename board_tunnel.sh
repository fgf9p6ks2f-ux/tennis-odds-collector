#!/usr/bin/env bash
# Live board exposure: board_server (localhost:8899) + cloudflared quick tunnel (public HTTPS).
# Publishes the current tunnel URL to docs/live_board.json (committed to GitHub) so the stable
# Pages board can discover it and redirect there; ntfy's the URL whenever it changes so the
# home-screen icon can be re-pointed if a reboot ever churns it. Runs under systemd.
set -u
cd "$HOME/tennis-odds-collector" || exit 1
GHREPO="github.com/fgf9p6ks2f-ux/tennis-odds-collector.git"
[ -f "$HOME/wnba-loop.env" ] && . "$HOME/wnba-loop.env"
URL_REPO="https://x-access-token:${GIT_PAT}@${GHREPO}"

# start the static+SSE server (background); keep a handle so we exit together
python3 board_server.py &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

publish() {   # $1 = tunnel url
  python3 - "$1" <<'PY'
import json, sys, datetime as dt, subprocess, os
url = sys.argv[1]
p = "docs/live_board.json"
try:
    cur = json.load(open(p)).get("url")
except Exception:
    cur = None
if cur == url:
    sys.exit(0)                                   # unchanged -> no commit/ping
json.dump({"url": url, "ts": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"},
          open(p, "w"))
os.system("git add docs/live_board.json && git commit -q -m 'live board url [skip ci]' "
          "&& git pull --rebase --autostash -q -X ours '%s' main "
          "&& git push -q '%s' main" % (os.environ.get("URL_REPO",""), os.environ.get("URL_REPO","")))
topic = os.environ.get("NTFY_TOPIC")
if topic:
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=f"Live board: {url}".encode(),
            headers={"Title": "Pickz live board", "Tags": "satellite"}), timeout=8)
    except Exception:
        pass
print("published", url)
PY
}
export URL_REPO NTFY_TOPIC

# run cloudflared, parse the assigned trycloudflare URL from its stderr, publish on (re)appearance
stdbuf -oL -eL cloudflared tunnel --no-autoupdate --url http://localhost:8899 2>&1 | \
while IFS= read -r line; do
  u=$(printf '%s' "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  [ -n "$u" ] && publish "$u"
done
