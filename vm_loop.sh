#!/usr/bin/env bash
cd "$HOME/tennis-odds-collector" || exit 1
export FD_DB="$(pwd)/wnba_lines.sqlite"
GHREPO="github.com/fgf9p6ks2f-ux/tennis-odds-collector.git"
git config user.name "odds-bot" 2>/dev/null; git config user.email "odds-bot@users.noreply.github.com" 2>/dev/null
URL="https://x-access-token:${GIT_PAT}@${GHREPO}"

push(){ git add -A -f 2>/dev/null
  # NEVER commit wnba_lines.sqlite: it's the VM-local WNBA lines DB, gitignored, and was
  # ballooning to 100MB (the 2-day prune lived in the now-disabled wnba-watch.yml and was
  # dropped on the VM migration). `git add -A -f` force-re-adds it every cycle despite the
  # ignore -> committing/hashing 100MB every 75s + git auto-gc repacking those blobs = the
  # swap-thrash. Unstage it here each cycle (keeps -f for the small caches the digests need).
  git rm --cached -q wnba_lines.sqlite wnba_glog_cache.json 2>/dev/null || true
  # NOTE: use `|| true`, NOT `|| return 0`. Since wnba_lines.sqlite (the file that changed every
  # cycle) is now excluded, many cycles have "nothing to commit" — returning early there SKIPS the
  # push, so any already-committed-but-unpushed commits (e.g. a fresh dashboard from fullscan, or a
  # push that failed during a thrash) pile up and origin/Pages/Actions all lag the VM. Always fall
  # through to pull+push so pending commits flush even on a no-change cycle.
  git commit -m "vm loop data [skip ci]" -q 2>/dev/null || true
  git pull --rebase --autostash -X theirs -q "$URL" main 2>/dev/null || {
    # Rebase wedged (usually an OOM kill mid-rebase on this 956MB box). Unwedge WITHOUT losing
    # data: a bare `reset --hard origin/main` rolled tracked DBs back to origin's older copies —
    # wnba_ledger.sqlite (live bets!), wnba_notified.txt (SEEN -> duplicate-ping storm), CLV.
    # Proven live 2026-07-17 04:00 (ate a dashboard-bake commit). So: reset to origin's tip to
    # unwedge, then REPLAY this VM's data files from the pre-reset tip and recommit. Code
    # (.py/.sh/.yml) is deliberately NOT replayed — fresh deploys from origin must win.
    git rebase --abort 2>/dev/null || true
    C=$(git rev-parse HEAD)
    git fetch -q "$URL" main 2>/dev/null && git reset --hard FETCH_HEAD -q 2>/dev/null
    git checkout "$C" -- "*.sqlite" 2>/dev/null || true
    git checkout "$C" -- "*.json"   2>/dev/null || true
    git checkout "$C" -- "*.txt"    2>/dev/null || true
    git checkout "$C" -- "*.md"     2>/dev/null || true
    git checkout "$C" -- docs/     2>/dev/null || true
    git add -A -f 2>/dev/null
    git rm --cached -q wnba_lines.sqlite wnba_glog_cache.json 2>/dev/null || true
    git commit -qm "vm loop data (replayed after failed rebase) [skip ci]" 2>/dev/null || true
    echo "[$(date +%H:%M)] rebase failed -> data replayed onto origin tip"
  }
  git push -q "$URL" HEAD:main 2>/dev/null || echo "[$(date +%H:%M)] push deferred"; }

collectors(){
  python3 fd_collect.py --wnba >/dev/null 2>&1 || true
  # dk_collect DISABLED on the VM (2026-07-16): DraftKings Akamai-blocks the Oracle datacenter
  # IP -> it 403s EVERY cycle (never once landed a row from here), but still spawns a curl_cffi
  # chrome-impersonation process each time = pure memory pressure on the 956MB box for nothing.
  # Re-enable only behind a residential proxy. (DK line-shopping runs fine from the Mac.)
  # python3 dk_collect.py --wnba >/dev/null 2>&1 || true
  python3 wnba_ledger.py --grade >/dev/null 2>&1 || true
  python3 wnba_clv.py --close >/dev/null 2>&1 || true
  prune_lines; board; }

# Keep wnba_lines.sqlite at its intended ~2-day WNBA window (the retention that lived in the
# now-disabled wnba-watch.yml, orphaned on the VM migration). Cheap DELETE each cycle; sqlite
# reuses freed pages so the file stays ~1-2MB after the one-time VACUUM done at deploy.
prune_lines(){ python3 - >/dev/null 2>&1 <<'PY' || true
import sqlite3
c=sqlite3.connect("wnba_lines.sqlite")
c.execute("DELETE FROM fd_lines WHERE collected_at < datetime('now','-2 days')")
c.commit(); c.close()
PY
}

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
    # next-day plays must fire fast too (user: post as soon as 1 out-confirmation + FD lines):
    # the full flagger also runs every ~40 hot ticks (~17 min) — it never ran in hot before,
    # so a new out + fresh next-day lines could sit unflagged for a whole evening.
    if [ $((hot_ticks % 40)) -eq 20 ]; then echo "[$(date +%H:%M)] full scan (hot)"; fullscan; fi
    sleep 25
  else
    # COLD PATH: normal 75s cycle; heavy full scan every 25 cold iterations.
    if [ "$was_hot" != "0" ]; then echo "[$(date +%H:%M)] <<< COLD window (75s cycle)"; fi
    was_hot=0; cold_i=$((cold_i+1))
    git pull -q "$URL" main 2>/dev/null || true
    collectors
    python3 wnba_watch.py >/dev/null 2>&1 || true
    if [ $((cold_i % 8)) -eq 1 ]; then echo "[$(date +%H:%M)] full scan (cold iter $cold_i)"; fullscan; fi
    push; sleep 60
  fi
done
