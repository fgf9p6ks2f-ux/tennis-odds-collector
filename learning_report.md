# Soft-spot learning report

_2026-07-08 06:05 UTC_

Benches a (sport, stat, src) market once it has ≥40 CLV-measured bets whose average CLV ≤ -1% — or, when CLV coverage is thin, ≥60 settled bets at ROI ≤ -5%. The ledger then stops betting it. CLV is the teacher; realized ROI is the backstop.

| sport | stat | src | CLV bets | avg CLV | W-L | ROI | status |
|---|---|---|---|---|---|---|---|
| mlb | strikeouts | direct | 10 | +4.08% | 3-7 | -32.8% | ⏳ learning |
| mlb | total_bases | direct | 9 | +4.90% | 1-6 | -65.0% | ⏳ learning |
| mlb | total_bases | model | 130 | +8.11% | 24-89 | -15.1% | ✅ green |
| tennis | player_games | model | 0 | — | 0-0 | — | ⏳ learning |

**Benched markets:** none

