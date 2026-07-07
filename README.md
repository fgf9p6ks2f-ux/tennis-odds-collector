# tennis-odds-collector

Serverless collector that snapshots Pinnacle tennis odds (match winner, set total
o/u 2.5, total games) every ~30 min via GitHub Actions and commits them to
`odds.sqlite`. No server, no billing.

Self-contained: `collect.py` + `pinnacle.py` + `store.py` (needs only `requests` +
`sqlite3`). Source of truth lives in the main project's
`src/tennisbet/oddsfeed/` — re-copy those modules here if the logic changes.

## Get the data for analysis

```bash
git pull                                   # newest odds.sqlite
cp odds.sqlite ~/tennis-odds/odds.sqlite   # where the analysis tools read it
```

The workflow is `.github/workflows/collect-odds.yml`. Run it manually anytime from the
repo's **Actions** tab → "collect-odds" → "Run workflow".
