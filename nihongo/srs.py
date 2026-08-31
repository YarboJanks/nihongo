"""Minimal SM-2-lite spaced repetition scheduler, shared by kana drills and
vocab/grammar review.
"""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class SrsState:
    ease: float
    interval_days: float
    reps: int


def review(state: SrsState, quality: int, today: date) -> tuple[SrsState, date]:
    """quality: 0-5, where <3 counts as a miss and resets the interval."""
    quality = max(0, min(5, quality))

    if quality < 3:
        reps = 0
        interval_days = 1.0
    else:
        reps = state.reps + 1
        if reps == 1:
            interval_days = 1.0
        elif reps == 2:
            interval_days = 6.0
        else:
            interval_days = round(state.interval_days * state.ease, 1)

    ease = state.ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ease = max(1.3, ease)

    new_state = SrsState(ease=ease, interval_days=interval_days, reps=reps)
    due = today + timedelta(days=interval_days)
    return new_state, due
