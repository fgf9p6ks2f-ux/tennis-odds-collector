# WNBA injury watcher — cloud speed layer

The whole edge is **latency**: beat the book to reprice after an injury drops. GitHub's
scheduled cron is throttled (real cadence ~5–20 min), and a local Mac poller only runs
while the Mac is awake. This is an always‑on cloud worker that polls **every minute**,
independent of both, and fires an urgent phone push the instant a key player is newly
ruled out.

## What it does

Every minute it fetches the ESPN injury feed + tonight's slate, and the moment a key
player (≥20 mpg, on a team playing tonight) newly flips to **Out/Doubtful** it:

1. pushes an urgent ntfy alert naming who just dropped (`JUST IN: C.Clark Out (IND)`), and
2. optionally kicks the GitHub scan (`wnba-props.yml`) to push the detailed +EV
   beneficiary spots a minute later.

Step 1 is the speed‑critical signal and needs only your ntfy topic. Step 2 is an add‑on
that needs a GitHub token.

## Setup (Val Town — free, no credit card, ~5 min)

1. Go to **val.town** and sign in (GitHub login).
2. **New val → Cron.** Set the schedule to `* * * * *` (every minute).
   - *(Free tier caps cron frequency; if it won't accept every‑minute, use `*/2 * * * *`
     — every 2 min is still far faster than GitHub.)*
3. Paste the contents of [`valtown_wnba_watch.ts`](valtown_wnba_watch.ts) into the val.
4. In the val's **Environment Variables** (or account settings → Environment Variables), add:
   - `NTFY_TOPIC` — your ntfy topic string (the exact one your phone is subscribed to).
     **Required.**
   - `GH_TOKEN` — *(optional, for the detailed‑spots scan)* a GitHub **fine‑grained
     personal access token** scoped to this repo with **Actions: Read and write**.
     Create at github.com → Settings → Developer settings → Fine‑grained tokens.
5. Save. Watch the val's run log: the first run prints `cold start — baselined N`, then
   it goes quiet until something new breaks.

That's it — no deploy step, no card, always on.

## Notes

- **"Key player" data** (who's ≥20 mpg and on which team) is read from this repo's public
  `wnba_players_cache.json`, refreshed by the GitHub workflows — so no season‑stat math
  runs in the cloud.
- **No double‑pushes with GitHub:** the cloud worker sends the *news* ("X is out");
  the GitHub scan sends the *spots*. Different messages, both wanted.
- **Cloudflare Workers** is an equally‑good free alternative (cron triggers, no card) if
  you prefer it — the same fetch/diff logic ports directly; swap Val Town's `blob` for a
  Workers KV namespace for the state.
- The instant‑news push is the point: a sharp bettor who knows "Clark is out" can act on
  the beneficiaries immediately; the computed spots follow.
