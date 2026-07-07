"""MLB player-props models. First target: pitcher strikeouts (over/under).

Reuses the tennis-betting philosophy: a calibrated projection vs the sharp (Pinnacle)
line, measured by CLV, bet at soft books (FanDuel/DraftKings). MLB's advantage: box-score
truth is free + instant, so the projection backtests against real outcomes immediately.
"""
