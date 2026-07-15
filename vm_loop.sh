#!/usr/bin/env bash
cd "$HOME/tennis-odds-collector" || exit 1
export FD_DB="$(pwd)/wnba_lines.sqlite"
GHREPO="github.com/fgf9p6ks2f-ux/tennis-odds-collector.git"
git config user.name "odds-bot" 2>/dev/null; git config user.email "odds-bot@users.noreply.github.com" 2>/dev/null
URL="https://x-access-token:${GIT_PAT}@${GHREPO}"
push(){ git add -A -f 2>/dev/null; git commit -m "vm loop data [skip ci]" -q 2>/dev/null || return 0
  git pull --rebase --autostash -X theirs -q "$URL" main 2>/dev/null || { git rebase --abort 2>/dev/null||true; git reset --hard origin/main -q 2>/dev/null||true; }
  git push -q "$URL" HEAD:main 2>/dev/null || echo "[$(date +%H:%M)] push deferred"; }
echo "[$(date)] wnba-loop up (topic:$([ -n "$NTFY_TOPIC" ]&&echo yes||echo NO) pat:$([ -n "$GIT_PAT" ]&&echo yes||echo NO))"
i=0
while true; do i=$((i+1))
  git pull -q "$URL" main 2>/dev/null || true
  python3 fd_collect.py --wnba >/dev/null 2>&1 || true
  python3 dk_collect.py --wnba >/dev/null 2>&1 || true
  python3 wnba_watch.py >/dev/null 2>&1 || true
  python3 wnba_ledger.py --grade >/dev/null 2>&1 || true
  python3 wnba_clv.py --close >/dev/null 2>&1 || true
  if [ $((i % 25)) -eq 1 ]; then echo "[$(date +%H:%M)] full scan (iter $i)"
    python3 wnba_alert.py >/dev/null 2>&1 || true
    python3 dashboard.py >/dev/null 2>&1 || true
    python3 wnba_ledger.py --train >/dev/null 2>&1 || true
    python3 wnba_context_report.py >/dev/null 2>&1 || true; fi
  push; sleep 60
done
