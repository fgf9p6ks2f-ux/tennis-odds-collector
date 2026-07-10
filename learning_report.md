# Soft-spot learning report

_2026-07-10 18:13 UTC_

Benches a (sport, stat, src) market once it has ≥40 CLV-measured bets whose average CLV ≤ -1% — or, when CLV coverage is thin, ≥60 settled bets at ROI ≤ -5%. The ledger then stops betting it. CLV is the teacher; realized ROI is the backstop.

| sport | stat | src | CLV bets | avg CLV | W-L | ROI | status |
|---|---|---|---|---|---|---|---|
| ebasketball | total | h2h | 3 | -6.98% | 5-1 | +51.8% | ⏳ learning |
| efootball | total | h2h | 0 | — | 1-1 | -4.5% | ⏳ learning |
| mlb | f5_total | direct | 0 | — | 3-0 | +92.4% | ⏳ learning |
| mlb | game_total | direct | 0 | — | 4-2 | +29.2% | ⏳ learning |
| mlb | strikeouts | direct | 22 | +3.70% | 9-12 | -10.6% | ⏳ learning |
| mlb | total_bases | direct | 52 | +4.93% | 12-30 | -42.9% | ✅ green |
| mlb | total_bases | model | 197 | +8.97% | 33-144 | -25.3% | 🛑 benched |
| tennis | player_games | model | 6 | +13.74% | 2-2 | -10.8% | ⏳ learning |

**Benched markets:** [['mlb', 'total_bases', 'model']]

