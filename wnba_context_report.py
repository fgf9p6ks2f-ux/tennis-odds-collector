"""Running context report: how the WNBA props perform conditioned on game total, underdog
size, and blowout — the game-script signals we now log (total + signed spread).

Backfills actual_total / actual_margin from ESPN finals into the ledger (idempotent), then
writes wnba_context.md: hit% by bucket, OVER vs UNDER split. Run after grading in wnba-props.

IMPORTANT: this is a MONITOR, not a live filter. The sample is small; a low-total / big-dog
OVER-fade must be BACKTESTED (~200+ graded) before it ever changes the model (golden rule).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import requests

try:
    import certifi
    _VERIFY = certifi.where()
except Exception:
    _VERIFY = True

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"
OUT = HERE / "wnba_context.md"
SB = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates={}"


def _finals(dates):
    """{YYYYMMDD: {frozenset(abbrs): {abbr: final_score}}} for FINAL games only."""
    out = {}
    for d in dates:
        try:
            j = requests.get(SB.format(d), timeout=20, verify=_VERIFY).json()
        except Exception:
            continue
        for e in j.get("events", []):
            comp = (e.get("competitions") or [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) != 2 or e.get("status", {}).get("type", {}).get("name") != "STATUS_FINAL":
                continue
            sc = {c["team"]["abbreviation"].upper(): int(c.get("score", 0) or 0) for c in cs}
            out.setdefault(d, {})[frozenset(sc)] = sc
    return out


def _side(r):
    return r["side"] or "over"


def _rec(rows):
    rows = [x for x in rows if x]
    if not rows:
        return "0-0 (--)"
    w = sum(1 for x in rows if x["result"] == _side(x))
    return f"{w}-{len(rows) - w} ({round(w / len(rows) * 100)}%)"


def _block(title, buckets):
    out = [f"**{title}**", "", "| bucket | all | OVER bets | UNDER bets |", "|---|---|---|---|"]
    for lbl, rows in buckets:
        ov = [r for r in rows if _side(r) == "over"]
        un = [r for r in rows if _side(r) == "under"]
        out.append(f"| {lbl} (n={len(rows)}) | {_rec(rows)} | {_rec(ov)} | {_rec(un)} |")
    return "\n".join(out) + "\n"


def main():
    if not LEDGER.exists():
        print("no ledger")
        return
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    have_cols = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
    for col in ("actual_total", "actual_margin"):
        if col not in have_cols:
            con.execute(f"ALTER TABLE predictions ADD COLUMN {col} REAL")
    con.commit()

    g = [dict(r) for r in con.execute(
        "SELECT rowid, * FROM predictions WHERE graded=1 AND result IN ('over','under')")]

    # backfill realized total/margin for graded rows still missing it
    need = [r for r in g if r.get("actual_total") is None]
    if need:
        finals = _finals(sorted({r["pred_date"].replace("-", "") for r in need}))
        for r in need:
            team = (r["team"] or "").upper()
            opp = (r["opp"] or "").upper()
            gm = finals.get(r["pred_date"].replace("-", ""), {}).get(frozenset({team, opp}))
            if gm and team in gm and opp in gm:
                at, am = gm[team] + gm[opp], gm[team] - gm[opp]
                con.execute("UPDATE predictions SET actual_total=?, actual_margin=? WHERE rowid=?",
                            (at, am, r["rowid"]))
                r["actual_total"], r["actual_margin"] = at, am
        con.commit()
    con.close()

    have = [r for r in g if r.get("actual_total") is not None]
    md = ["# WNBA prop context report",
          f"_updated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · "
          f"{len(g)} graded props ({len(have)} matched to finals)_", "",
          f"Overall **{_rec(g)}** · OVER {_rec([r for r in g if _side(r) == 'over'])} · "
          f"UNDER {_rec([r for r in g if _side(r) == 'under'])}", ""]

    pg = [r for r in g if r.get("total") is not None]
    if len(pg) >= 6:
        m = sorted(r["total"] for r in pg)[len(pg) // 2]
        md.append(_block(f"By PRE-GAME total line (median {m:g}) — the usable feature", [
            (f">={m:g}", [r for r in pg if r["total"] >= m]),
            (f"<{m:g}", [r for r in pg if r["total"] < m])]))

    sp = [r for r in g if r.get("spread") is not None]
    if len(sp) >= 6:
        md.append(_block("By UNDERDOG size (pre-game spread; +ve = dog)", [
            ("favorite (<0)", [r for r in sp if r["spread"] < 0]),
            ("dog 0-8", [r for r in sp if 0 <= r["spread"] < 8]),
            ("DOG 8+", [r for r in sp if r["spread"] >= 8])]))

    if have:
        tots = sorted(r["actual_total"] for r in have)
        lo, hi = tots[len(tots) // 3], tots[2 * len(tots) // 3]
        md.append(_block(f"By ACTUAL total (realized; terciles {lo:g}/{hi:g})", [
            (f">{hi:g}", [r for r in have if r["actual_total"] > hi]),
            (f"{lo:g}-{hi:g}", [r for r in have if lo <= r["actual_total"] <= hi]),
            (f"<{lo:g}", [r for r in have if r["actual_total"] < lo])]))
        md.append(_block("By blowout (|actual margin|)", [
            ("blowout 12+", [r for r in have if abs(r["actual_margin"]) >= 12]),
            ("close <12", [r for r in have if abs(r["actual_margin"]) < 12])]))

    md.append(f"> {len(g)} bets — **directional monitor, not a validated edge.** A low-total / "
              "big-dog OVER-fade needs a proper backtest (~200+ graded) before it changes the model.")
    OUT.write_text("\n".join(md))
    print(f"context report: {len(g)} graded ({len(have)} w/ finals) -> {OUT.name}")


if __name__ == "__main__":
    main()
