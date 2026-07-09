// WNBA injury watcher — cloud SPEED layer (Val Town Cron val, run every 1 min).
//
// The edge is latency: beat the book to reprice after an injury drops. This polls the
// ESPN injury feed + today's slate every minute (independent of GitHub's throttled cron
// and your Mac being awake), and the instant a KEY player on tonight's slate is newly
// ruled Out/Doubtful it fires an urgent ntfy push naming who dropped — then optionally
// kicks the GitHub scan to push the detailed +EV spots a minute later.
//
// SETUP (see cloud/README.md): paste into a new Val Town **Cron** val set to "* * * * *"
// (every minute), and add env vars in Val Town settings:
//   NTFY_TOPIC  (required)  — your ntfy topic, same one the phone is subscribed to
//   GH_TOKEN    (optional)  — a GitHub fine-grained PAT with Actions: read+write on the
//                             repo; lets the worker trigger the detailed-spots scan
//
// "Key player" (>=20 mpg + which team) is read from the repo's public roster cache, so
// no season-stat computation happens in the cloud.

const REPO = "fgf9p6ks2f-ux/tennis-odds-collector";
const ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba";
const PLAYERS_URL = `https://raw.githubusercontent.com/${REPO}/main/wnba_players_cache.json`;
const KEY_MIN = 20;

// Val Town persistent storage (survives between runs)
import { blob } from "https://esm.town/v/std/blob";
const STATE_KEY = "wnba_injury_state";
const INIT_KEY = "wnba_injury_initialized";

export default async function () {
  const NTFY = Deno.env.get("NTFY_TOPIC");
  const GH_TOKEN = Deno.env.get("GH_TOKEN");

  // today's slate in US Eastern (the slate date), non-final games only
  const etDate = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date()).replaceAll("-", "");
  const sb = await (await fetch(`${ESPN}/scoreboard?dates=${etDate}`)).json();
  const playing = new Set<string>();
  for (const e of sb.events ?? []) {
    if (e.status?.type?.state === "post") continue;
    for (const c of e.competitions?.[0]?.competitors ?? []) {
      const ab = c.team?.abbreviation;
      if (ab) playing.add(ab);
    }
  }
  if (playing.size === 0) return "no games on the slate — idle";

  // current injury list (Out / Doubtful)
  const injJson = await (await fetch(`${ESPN}/injuries`)).json();
  const inj: Record<string, string> = {};
  for (const t of injJson.injuries ?? []) {
    for (const p of t.injuries ?? []) {
      const nm = p.athlete?.displayName;
      const st = p.status;
      if (nm && (st === "Out" || st === "Doubtful")) inj[nm] = st;
    }
  }

  // key = >=20 mpg AND on a team playing tonight (from the repo's public roster cache)
  let players: Record<string, { team: string; min: number }> = {};
  try {
    players = (await (await fetch(PLAYERS_URL)).json()).players ?? {};
  } catch (_) { /* if the cache is briefly unavailable, skip this tick */ return "roster cache unavailable"; }
  const cur: Record<string, string> = {};
  for (const [nm, st] of Object.entries(inj)) {
    const p = players[nm];
    if (p && playing.has(p.team) && p.min >= KEY_MIN) cur[nm] = st;
  }

  const prev: Record<string, string> = (await blob.getJSON(STATE_KEY)) ?? {};
  const initialized = await blob.getJSON(INIT_KEY);
  await blob.setJSON(STATE_KEY, cur);
  if (!initialized) { await blob.setJSON(INIT_KEY, true); return `cold start — baselined ${Object.keys(cur).length}`; }

  // NEW = newly Out/Doubtful, or escalated Doubtful->Out, since the last poll
  const news = Object.entries(cur).filter(
    ([n, s]) => prev[n] !== s && !(prev[n] === "Out" && s === "Doubtful"),
  );
  if (news.length === 0) return `no new outs (${Object.keys(cur).length} known)`;

  const short = (n: string) => { const p = n.split(" "); return p.length >= 2 ? `${p[0][0]}.${p[p.length - 1]}` : n; };
  const label = news.map(([n, s]) => `${short(n)} ${s} (${players[n]?.team})`).join(", ");

  if (NTFY) {
    await fetch(`https://ntfy.sh/${NTFY}`, {
      method: "POST",
      body: `JUST IN: ${label}\nComputing beneficiary spots...`,
      headers: { Title: `WNBA news: ${label}`.slice(0, 120), Priority: "urgent", Tags: "rotating_light" },
    });
  }
  // kick the GitHub scan for the detailed +EV spots (optional; needs a PAT)
  if (GH_TOKEN) {
    await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/wnba-props.yml/dispatches`, {
      method: "POST",
      headers: { Authorization: `Bearer ${GH_TOKEN}`, Accept: "application/vnd.github+json", "User-Agent": "wnba-watch" },
      body: JSON.stringify({ ref: "main" }),
    });
  }
  return `PUSHED: ${label}`;
}
