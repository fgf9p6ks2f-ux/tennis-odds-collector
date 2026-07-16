#!/usr/bin/env bash
cd "$HOME/tennis-odds-collector" || exit 1
export FD_DB="$(pwd)/wnba_lines.sqlite"
GHREPO="github.com/fgf9p6ks2f-ux/tennis-odds-collector.git"
git config user.name "odds-bot" 2>/dev/null; git config user.email "odds-bot@users.noreply.github.com" 2>/dev/null
URL="https://x-access-token:${GIT_PAT}@${GHREPO}"

push(){ git add -A -f 2>/dev/null; git commit -m "vm loop data [skip ci]" -q 2>/dev/null || return 0
  git pull --rebase --autostash -X theirs -q "$URL" main 2>/dev/null || { git rebase --abort 2>/dev/null||true; git reset --hard origin/main -q 2>/dev/null||true; }
  git push -q "$URL" HEAD:main 2>/dev/null || echo "[$(date +%H:%M)] push deferred"; }

collectors(){
  python3 fd_collect.py --wnba >/dev/null 2>&1 || true
  python3 dk_collect.py --wnba >/dev/null 2>&1 || true
  python3 wnba_ledger.py --grade >/dev/null 2>&1 || true
  python3 wnba_clv.py --close >/dev/null 2>&1 || true
  board; }

# TT Elite FanDuel.ca total-line board — the VM is the only host that can reach FanDuel.ca
# (Actions' US IP is geo-blocked). Writes fd_board.json; push() commits it to this PUBLIC
# repo, and tt-elite's daily.yml fetches it via raw.githubusercontent to grade Elite at real
# lines. THROTTLED to >=4 min between fetches (self-timed, not per-cycle) so it adds minimal
# memory pressure on the 956MB box — TT lines don't need 75s freshness.
board(){ local f=/tmp/.fd_board_last now last; now=$(date +%s); last=$(cat "$f" 2>/dev/null || echo 0)
  [ $((now - last)) -lt 240 ] && return 0
  python3 fd_tt.py --board --captured-at "$(date -u +%FT%TZ)" >/dev/null 2>&1 && echo "$now" > "$f" || true; }

fullscan(){
  python3 wnba_alert.py >/dev/null 2>&1 || true
  python3 dashboard.py >/dev/null 2>&1 || true
  python3 wnba_ledger.py --train >/dev/null 2>&1 || true
  python3 wnba_context_report.py >/dev/null 2>&1 || true; }

# exit 0 when a game is live or tips within ~75min -> switch to fast scratch polling
in_hot(){ python3 hot_window.py >/dev/null 2>&1; }

# liveness heartbeat: one parentless commit force-pushed to the `heartbeat` branch each cycle
# (no history growth). The Actions vm-watchdog alerts if this stops updating (VM down / token dead).
beat(){
  local c b t k
  c="$(date -u +%s) $(date -u +%FT%TZ)"
  b=$(printf '%s\n' "$c" | git hash-object -w --stdin 2>/dev/null) || return 0
  t=$(printf '100644 blob %s\theartbeat.txt\n' "$b" | git mktree 2>/dev/null) || return 0
  k=$(printf 'vm heartbeat %s\n' "$c" | git commit-tree "$t" 2>/dev/null) || return 0
  git push -q --force "$URL" "$k:refs/heads/heartbeat" 2>/dev/null || true; }

echo "[$(date)] wnba-loop up (topic:$([ -n "$NTFY_TOPIC" ]&&echo yes||echo NO) pat:$([ -n "$GIT_PAT" ]&&echo yes||echo NO))"
i=0; hot_ticks=0; cold_i=0; was_hot=2
while true; do i=$((i+1))
  beat
  if in_hot; then
    # HOT PATH: wnba_watch (scratch detector -> instant ntfy) every ~25s.
    # Refresh odds/grade + dashboard + push every 3rd tick (~75s) so the board tracks the action.
    if [ "$was_hot" != "1" ]; then echo "[$(date +%H:%M)] >>> HOT window (25s scratch polling)"; hot_ticks=0; fi
    was_hot=1; hot_ticks=$((hot_ticks+1))
    python3 wnba_watch.py >/dev/null 2>&1 || true
    if [ $((hot_ticks % 3)) -eq 0 ]; then
      git pull -q "$URL" main 2>/dev/null || true
      collectors
      python3 dashboard.py >/dev/null 2>&1 || true
      push
    fi
    sleep 25
  else
    # COLD PATH: normal 75s cycle; heavy full scan every 25 cold iterations.
    if [ "$was_hot" != "0" ]; then echo "[$(date +%H:%M)] <<< COLD window (75s cycle)"; fi
    was_hot=0; cold_i=$((cold_i+1))
    git pull -q "$URL" main 2>/dev/null || true
    collectors
    python3 wnba_watch.py >/dev/null 2>&1 || true
    if [ $((cold_i % 25)) -eq 1 ]; then echo "[$(date +%H:%M)] full scan (cold iter $cold_i)"; fullscan; fi
    push; sleep 60
  fi
done
