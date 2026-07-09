"""Auto-updating phone dashboard — today's WNBA plays, served free via GitHub Pages.

Reads the injury-autobetter ledger (wnba_ledger.sqlite: today's flagged spots + grades)
and renders a mobile HTML page to docs/index.html. The WNBA workflows regenerate + commit
it every run, GitHub Pages serves it, and the page self-refreshes — so it stays current on
your phone with no Mac needed.

    python dashboard.py            # -> docs/index.html
"""
from __future__ import annotations

import datetime as dt
import html
import sqlite3
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    MT = ZoneInfo("America/Denver")
except Exception:
    MT = dt.timezone(dt.timedelta(hours=-6))

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "wnba_ledger.sqlite"
OUT = HERE / "docs" / "index.html"
STATKEY = {"points": "PTS", "rebounds": "REB", "assists": "AST"}


def _am(dec):
    dec = float(dec)
    return f"+{round((dec-1)*100)}" if dec >= 2 else f"{round(-100/(dec-1))}"


def _short(n):
    p = (n or "").split()
    return f"{p[0][0]}.{p[-1]}" if len(p) >= 2 else n


def _load(mt_date):
    if not LEDGER.exists():
        return [], (0, 0, 0.0, 0)
    con = sqlite3.connect(LEDGER)
    rows = con.execute(
        "SELECT out_player,player,stat,line,odds,ev,elev_avg,season_avg,d_min,driver,"
        "total,opp_def,result,actual,stale FROM predictions WHERE pred_date=? "
        "ORDER BY ev DESC", (mt_date,)).fetchall()
    # record since the 7/9 reset (all graded)
    g = con.execute("SELECT result,odds FROM predictions WHERE graded=1 AND pred_date>='2026-07-09'").fetchall()
    con.close()
    dec = [r for r in g if r[0] in ("over", "under")]
    w = sum(1 for r in dec if r[0] == "over")
    u = sum((r[1] - 1) if r[0] == "over" else -1 for r in dec)
    pend = sum(1 for r in rows if r[12] is None)
    return rows, (w, len(dec) - w, u, pend)


def _card(r):
    out_p, player, stat, line, odds, ev, elev, season, d_min, driver, total, opp_def, \
        result, actual, stale = r
    lbl = STATKEY.get(stat, stat.upper())
    if result == "over":
        badge, cls = f"HIT · {actual:g}", "hit"
    elif result in ("under", "push"):
        badge, cls = f"{'MISS' if result=='under' else 'PUSH'} · {actual:g}", "miss"
    else:
        badge, cls = "pending", "pend"
    drv = {"points": "FGA", "rebounds": "REB", "assists": "AST"}.get(stat, "")
    sig = []
    if driver is not None:
        sig.append(f"{drv} {driver:+g}")
    if d_min is not None:
        sig.append(f"min {d_min:+g}")
    if total:
        sig.append(f"O/U {total:g}")
    if opp_def:
        sig.append(f"opp {opp_def:g}")
    thin = float(ev) > 0.35
    return f"""
    <div class="card {cls}">
      <div class="row">
        <div class="play">{html.escape(_short(player))} <b>{lbl} o{line:g}</b> <span class="odds">{_am(odds)}</span></div>
        <div class="badge {cls}">{badge}</div>
      </div>
      <div class="sub">{html.escape(_short(out_p))} out · elev {elev:g} vs season {season:g} · +{ev*100:.0f}% EV{' ⚠︎ thin' if thin else ''}</div>
      <div class="sig">{' · '.join(html.escape(s) for s in sig)}</div>
    </div>"""


def build():
    now = dt.datetime.now(dt.timezone.utc).astimezone(MT)
    mt_date = now.date().isoformat()
    rows, (w, l, u, pend) = _load(mt_date)
    cards = "\n".join(_card(r) for r in rows) if rows else \
        '<div class="empty">No plays flagged yet today. The watcher checks every ~60s and fills this in the moment a key player is ruled out.</div>'
    rec = f"{w}-{l} · {u:+.1f}u" if (w + l) else "0-0"
    doc = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="90">
<title>Today's Plays</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  body {{ margin:0; background:#0b0e14; color:#e6eaf0; font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:640px; margin:0 auto; padding:16px 14px 40px; }}
  h1 {{ font-size:20px; margin:4px 0 2px; }}
  .meta {{ color:#8b94a3; font-size:12px; margin-bottom:14px; }}
  .rec {{ display:flex; gap:10px; margin-bottom:16px; }}
  .rec div {{ flex:1; background:#141a24; border:1px solid #222b38; border-radius:12px; padding:10px 12px; }}
  .rec .k {{ color:#8b94a3; font-size:11px; text-transform:uppercase; letter-spacing:.04em; }}
  .rec .v {{ font-size:19px; font-weight:700; margin-top:2px; }}
  h2 {{ font-size:13px; color:#8b94a3; text-transform:uppercase; letter-spacing:.05em; margin:18px 0 8px; }}
  .card {{ background:#141a24; border:1px solid #222b38; border-left:3px solid #3b82f6; border-radius:12px; padding:11px 13px; margin-bottom:9px; }}
  .card.hit {{ border-left-color:#22c55e; }} .card.miss {{ border-left-color:#ef4444; }} .card.pend {{ border-left-color:#3b82f6; }}
  .row {{ display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }}
  .play {{ font-size:16px; }} .play b {{ font-weight:700; }}
  .odds {{ color:#3b82f6; font-weight:700; }}
  .badge {{ font-size:11px; font-weight:700; padding:3px 8px; border-radius:20px; white-space:nowrap; }}
  .badge.hit {{ background:#0f2e1a; color:#4ade80; }} .badge.miss {{ background:#2e1315; color:#f87171; }} .badge.pend {{ background:#182234; color:#7aa2e3; }}
  .sub {{ color:#aab3c1; font-size:13px; margin-top:5px; }}
  .sig {{ color:#7b8494; font-size:12px; margin-top:3px; }}
  .empty {{ color:#8b94a3; background:#141a24; border:1px solid #222b38; border-radius:12px; padding:18px; text-align:center; }}
  .foot {{ color:#5b6472; font-size:11px; text-align:center; margin-top:20px; }}
</style></head><body><div class="wrap">
  <h1>🏀 Today's Plays</h1>
  <div class="meta">WNBA injury edge · updated {now:%b %-d, %-I:%M %p} MT · auto-refreshes</div>
  <div class="rec">
    <div><div class="k">Today's board</div><div class="v">{len(rows)} spot{'s' if len(rows)!=1 else ''}</div></div>
    <div><div class="k">Record (since 7/9)</div><div class="v">{rec}</div></div>
    <div><div class="k">Pending</div><div class="v">{pend}</div></div>
  </div>
  <h2>Flagged spots</h2>
  {cards}
  <div class="foot">Auto-generated on GitHub · self-refreshing · no device required</div>
</div></body></html>"""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(doc)
    print(f"dashboard: {len(rows)} plays, record {rec}, {pend} pending -> {OUT}")


if __name__ == "__main__":
    build()
