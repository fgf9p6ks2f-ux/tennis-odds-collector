# Soft-spot learning report

_2026-07-08 18:07 UTC_

Benches a (sport, stat, src) market once it has ≥40 CLV-measured bets whose average CLV ≤ -1% — or, when CLV coverage is thin, ≥60 settled bets at ROI ≤ -5%. The ledger then stops betting it. CLV is the teacher; realized ROI is the backstop.

| sport | stat | src | CLV bets | avg CLV | W-L | ROI | status |
|---|---|---|---|---|---|---|---|
| ebasketball | total | h2h | 0 | — | 2-1 | +20.1% | ⏳ learning |
| efootball | total | h2h | 0 | — | 1-1 | -4.5% | ⏳ learning |
| mlb | game_total | direct | 0 | — | 0-0 | — | ⏳ learning |
| mlb | strikeouts | direct | 10 | +4.08% | 4-7 | -16.9% | ⏳ learning |
| mlb | total_bases | direct | 9 | +4.90% | 1-8 | -72.8% | ⏳ learning |
| mlb | total_bases | model | 130 | +8.11% | 25-96 | -16.5% | 🛑 benched |
| tennis | player_games | model | 4 | +17.01% | 0-0 | — | ⏳ learning |

**Benched markets:** [['mlb', 'total_bases', 'model']]

