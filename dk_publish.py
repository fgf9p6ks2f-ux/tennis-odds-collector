"""Mac-side DraftKings publisher — the residential-IP half of the two-book pipeline.

DraftKings Akamai-blocks datacenter IPs (Oracle VM 403s every request; Actions is flaky),
but collection works perfectly from the Mac. This script runs on a launchd timer (~5 min):

  1. dk_collect --wnba  -> fresh book='dk' rows in the LOCAL fanduel_props.sqlite
  2. snapshot the current DK WNBA board -> dk_board.json (small, Mac-OWNED file)
  3. commit+push ONLY dk_board.json when its content changed (hash guard, autostash
     rebase + retry so it never fights the VM's data commits)

The VM ingests dk_board.json each loop cycle (dk_ingest.py) into its own wnba_lines.sqlite,
which lights up every existing consumer unchanged: card book-logos/best-price
(dashboard._book_prices), posted_props' book-aware quotes, and CLV's alt-book column.
Flag/record odds remain FanDuel's (the executable book); DK is price context + the
reprice-race second target.

    python3 dk_publish.py            # one publish pass
    python3 dk_publish.py --loop     # for testing; production uses launchd StartInterval
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOARD = HERE / "dk_board.json"
QUIET_START, QUIET_END = 1, 6          # local hours with no games/lines worth publishing


def collect():
    r = subprocess.run([sys.executable, str(HERE / "dk_collect.py"), "--wnba"],
                       capture_output=True, text=True, timeout=240, cwd=HERE)
    ok = r.returncode == 0
    print(("dk_collect ok: " if ok else "dk_collect FAILED: ") + (r.stdout + r.stderr).strip()[-120:])
    return ok


def snapshot():
    """Freshest DK quote per (event, player, stat, line, side) from the last 15 min."""
    con = sqlite3.connect(HERE / "fanduel_props.sqlite")
    con.row_factory = sqlite3.Row
    cut = (dt.datetime.utcnow() - dt.timedelta(minutes=15)).isoformat()[:19]
    rows = con.execute(
        "SELECT event, player, stat, line, side, odds, MAX(collected_at) ca FROM fd_lines "
        "WHERE book='dk' AND sport='wnba' AND collected_at > ? "
        "GROUP BY event, player, stat, line, side", (cut,)).fetchall()
    con.close()
    return [{"event": r["event"], "player": r["player"], "stat": r["stat"],
             "line": r["line"], "side": r["side"], "odds": r["odds"]} for r in rows]


def publish(lines):
    body = {"ts": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "sport": "wnba", "book": "dk", "lines": sorted(
                lines, key=lambda x: (x["player"], x["stat"], x["line"], x["side"]))}
    payload = json.dumps({k: body[k] for k in ("sport", "book", "lines")}, sort_keys=True)
    digest = hashlib.sha1(payload.encode()).hexdigest()
    old = ""
    if BOARD.exists():
        try:
            old = json.loads(BOARD.read_text()).get("hash", "")
        except (ValueError, OSError):
            pass
    if digest == old:
        print(f"no change ({len(lines)} lines) — skip commit")
        return
    body["hash"] = digest
    BOARD.write_text(json.dumps(body))
    for attempt in range(4):
        try:
            subprocess.run(["git", "add", "dk_board.json"], cwd=HERE, check=True, timeout=30)
            subprocess.run(["git", "commit", "-q", "-m", "dk board [skip ci]"],
                           cwd=HERE, check=False, timeout=30)
            subprocess.run(["git", "pull", "--rebase", "--autostash", "-q", "-X", "ours",
                            "origin", "main"], cwd=HERE, check=True, timeout=90)
            subprocess.run(["git", "push", "-q", "origin", "main"], cwd=HERE, check=True, timeout=90)
            print(f"published {len(lines)} DK lines")
            return
        except subprocess.CalledProcessError:
            continue
    print("push failed after retries — next timer run retries")


def main():
    h = dt.datetime.now().hour
    if QUIET_START <= h < QUIET_END:
        print(f"quiet hours ({h}h) — skip")
        return
    if collect():
        publish(snapshot())


if __name__ == "__main__":
    main()
