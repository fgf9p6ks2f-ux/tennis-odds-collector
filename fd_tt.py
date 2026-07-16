"""FanDuel TT Elite total-points line collector.

The model's flags/grades were pinned to a FIXED 74.5 proxy, but FanDuel prices each
match on its OWN line (observed range 56.5–77.5) — so a "over 74.5" flag was scoring a
bet that isn't on the board. This pulls FanDuel's real Total Points line + over/under
odds for every upcoming TT Elite match and stores a timestamped snapshot, so flagging
and grading can use the line the bet is ACTUALLY available at.

    python fd_tt.py            # dry-run: print current FD lines, write nothing
    python fd_tt.py --write    # upsert a snapshot row per match into tt.sqlite fd_lines

GEO NOTE: Table tennis lives on FanDuel.CA (Canada), not .com. The sbapi is reachable
from a Canadian residential IP (verified: the user's Mac in Alberta) but DENIES the
in-app browser's datacenter IP. GitHub Actions (US) is almost certainly geo-blocked;
whether the Oracle Toronto VM's datacenter IP is allowed is still TBD. Pick the host
accordingly — this script itself is host-agnostic.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import unicodedata
from pathlib import Path

from curl_cffi import requests as cr

HERE = Path(__file__).resolve().parent
DB = HERE / "tt.sqlite"
BOARD = HERE / "fd_board.json"          # committed by the VM; read by check_today on Actions

AK = "FhMFpcPWXMeyZxOx"
REGION = "ab"                                   # Alberta — where TT Elite is priced
BASE = f"https://sbapi.{REGION}.sportsbook.fanduel.ca/api"
TT_EVENT_TYPE = 2593174                         # "Table Tennis" eventTypeId
# The only TT competition FanDuel.ca carries today. Kept as a guard so a future league
# appearing under Table Tennis doesn't get silently mixed into TT Elite grading.
TT_ELITE_COMP = 12462788                        # "TT Elite Series - Men"


def _get(url):
    return cr.get(url, impersonate="chrome", timeout=25).json()


def _amer(runner):
    o = ((runner.get("winRunnerOdds") or {}).get("americanDisplayOdds") or {})
    return o.get("americanOdds")


def norm(name: str) -> str:
    """Fold to ascii-lower for cross-source name matching (24live vs FanDuel)."""
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.lower().split())


def list_events():
    """[(event_id, p1, p2, open_date, competition_id)] for every TT event on the board."""
    page = _get(f"{BASE}/content-managed-page?page=SPORT&eventTypeId={TT_EVENT_TYPE}&_ak={AK}")
    evs = page.get("attachments", {}).get("events", {})
    out = []
    for eid, e in evs.items():
        nm = e.get("name") or ""
        if " v " not in nm:                     # not a head-to-head event
            continue
        p1, p2 = (s.strip() for s in nm.split(" v ", 1))
        out.append((int(eid), p1, p2, e.get("openDate"), e.get("competitionId")))
    return sorted(out, key=lambda r: r[3] or "")


def total_line(event_id):
    """Main 'Total Points' market for one event -> (line, over_odds, under_odds) or None."""
    ev = _get(f"{BASE}/event-page?eventId={event_id}&_ak={AK}")
    mks = ev.get("attachments", {}).get("markets", {})
    for m in mks.values():
        if (m.get("marketName") or "").strip().lower() != "total points":
            continue
        line = over = under = None
        for r in m.get("runners", []):
            rn = (r.get("runnerName") or "").lower()
            if "over" in rn:
                over, line = _amer(r), r.get("handicap")
            elif "under" in rn:
                under = _amer(r)
        if line is not None:
            return (line, over, under)
    return None


def collect():
    """[(event_id, p1, p2, open_date, comp, line, over_odds, under_odds)] for TT Elite."""
    rows = []
    for eid, p1, p2, od, comp in list_events():
        if comp != TT_ELITE_COMP:               # guard: only TT Elite grades against these
            continue
        tl = total_line(eid)
        if tl is None:
            continue
        line, over, under = tl
        rows.append((eid, p1, p2, od, comp, line, over, under))
    return rows


def ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS fd_lines (
            captured_at TEXT NOT NULL,          -- UTC ISO, when this snapshot was pulled
            event_id    INTEGER NOT NULL,
            p1          TEXT NOT NULL,
            p2          TEXT NOT NULL,
            p1_norm     TEXT NOT NULL,          -- ascii-folded, for 24live join
            p2_norm     TEXT NOT NULL,
            open_date   TEXT,                   -- match start, UTC ISO
            line        REAL NOT NULL,          -- FanDuel Total Points handicap
            over_odds   INTEGER,                -- american
            under_odds  INTEGER,
            book        TEXT NOT NULL DEFAULT 'fd',
            PRIMARY KEY (event_id, captured_at)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fd_lines_norm ON fd_lines(p1_norm, p2_norm)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fd_lines_open ON fd_lines(open_date)")


def write(rows, captured_at):
    con = sqlite3.connect(DB)
    try:
        ensure_table(con)
        for eid, p1, p2, od, comp, line, over, under in rows:
            con.execute(
                "INSERT OR REPLACE INTO fd_lines "
                "(captured_at,event_id,p1,p2,p1_norm,p2_norm,open_date,line,over_odds,under_odds,book) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'fd')",
                (captured_at, eid, p1, p2, norm(p1), norm(p2), od, line, over, under))
        con.commit()
    finally:
        con.close()


def write_board(rows, captured_at, path=BOARD):
    """Overwrite fd_board.json with the CURRENT board — a text artifact the VM commits and
    Actions reads (tt.sqlite is binary/merge-hostile across the two writers). Grading doesn't
    read this; check_today stamps each bet with the line+odds at flag time from here."""
    board = {"captured_at": captured_at,
             "matches": [{"event_id": eid, "p1": p1, "p2": p2,
                          "p1_norm": norm(p1), "p2_norm": norm(p2), "open_date": od,
                          "line": line, "over_odds": over, "under_odds": under}
                         for eid, p1, p2, od, comp, line, over, under in rows]}
    path.write_text(json.dumps(board, indent=1))


def load_board(path=BOARD):
    """{frozenset(p1_norm,p2_norm): match_dict} for name-joining, or {} if no board yet."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {frozenset((m["p1_norm"], m["p2_norm"])): m for m in data.get("matches", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="upsert a snapshot into tt.sqlite")
    ap.add_argument("--board", action="store_true", help="write fd_board.json (current board)")
    ap.add_argument("--captured-at", help="snapshot UTC timestamp (the loop passes it)")
    args = ap.parse_args()

    rows = collect()
    print(f"=== FanDuel.ca TT Elite Total Points — {len(rows)} matches ===")
    print(f"{'matchup':<42}{'line':>6}{'over':>7}{'under':>7}")
    for eid, p1, p2, od, comp, line, over, under in rows:
        print(f"{p1+' v '+p2:<42}{line:>6}{str(over):>7}{str(under):>7}")

    if args.write or args.board:
        ts = args.captured_at
        if not ts:
            # caller supplies time; keep this file free of wall-clock so it stays testable
            raise SystemExit("--write/--board require --captured-at (UTC ISO); the loop passes it")
        if args.write:
            write(rows, ts)
        if args.board:
            write_board(rows, ts)
        print(f"\nwrote {len(rows)} matches @ {ts}"
              f"{' (sqlite)' if args.write else ''}{' (board)' if args.board else ''}")


if __name__ == "__main__":
    main()
