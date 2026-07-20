# Soft-spot learning report

_2026-07-20 19:16 UTC_

Benches a (sport, stat, src) market once it has ≥40 CLV-measured bets whose average CLV ≤ -1% — or, when CLV coverage is thin, ≥60 settled bets at ROI ≤ -5%. The ledger then stops betting it. CLV is the teacher; realized ROI is the backstop.

| sport | stat | src | CLV bets | avg CLV | W-L | ROI | status |
|---|---|---|---|---|---|---|---|
| ebasketball | total | h2h | 21 | -4.65% | 23-13 | +16.5% | ⏳ learning |
| efootball | total | h2h | 0 | — | 2-1 | +24.7% | ⏳ learning |
| mlb | f5_total | direct | 0 | — | 16-5 | +44.7% | ⏳ learning |
| mlb | game_total | direct | 0 | — | 29-24 | +3.9% | ⏳ learning |
| mlb | strikeouts | direct | 28 | +3.87% | 11-18 | -20.4% | ⏳ learning |
| mlb | total_bases | direct | 56 | +5.03% | 18-36 | -31.0% | ✅ green |
| mlb | total_bases | model | 197 | +8.97% | 33-144 | -25.3% | 🛑 benched |
| tennis | player_games | model | 10 | +11.06% | 3-7 | -45.4% | ⏳ learning |

**Benched markets:** [['mlb', 'total_bases', 'model']]

