"""Follow budget: N follows per rolling window, a rolling 24h cap, and a random gap between follows."""

from __future__ import annotations

import math
import random

DAY = 86_400
HOUR = 3_600
WARMUP_HOURS = 8  # a warming-up account spreads its day's follows over at least this many hours


def warmup_caps(daily_limit: int, start: int, age_seconds: float) -> tuple[int, int | None]:
    """(daily cap, hourly cap or None) for an account `age_seconds` after it was added.

    Day 1 allows `start`, each later day twice the day before, until `daily_limit`. While warming up
    the day's follows are also spread out (an hourly cap), since a burst is what got new accounts locked.
    """
    if start <= 0:
        return daily_limit, None
    day = max(0, int(age_seconds // DAY))
    cap = daily_limit if day >= 16 else min(daily_limit, start * 2 ** day)
    if cap >= daily_limit:
        return daily_limit, None
    return cap, max(2, math.ceil(cap / WARMUP_HOURS))


def next_allowed_at(
    action_times: list[float],
    now: float,
    *,
    window_limit: int,
    window_seconds: int,
    daily_limit: int,
    hourly_limit: int | None = None,
    not_before: float = 0.0,
) -> float:
    """Earliest time another follow may be sent.

    `action_times` are timestamps of follow requests already sent (any order).
    `not_before` carries the per-follow random gap and any pause.
    """
    ready = max(now, not_before)

    in_window = sorted(t for t in action_times if t > now - window_seconds)
    if window_limit <= 0:
        return float("inf")
    if len(in_window) >= window_limit:
        ready = max(ready, in_window[len(in_window) - window_limit] + window_seconds)

    if hourly_limit is not None:
        in_hour = sorted(t for t in action_times if t > now - HOUR)
        if len(in_hour) >= hourly_limit:
            ready = max(ready, in_hour[len(in_hour) - hourly_limit] + HOUR)

    in_day = sorted(t for t in action_times if t > now - DAY)
    if daily_limit <= 0:
        return float("inf")
    if len(in_day) >= daily_limit:
        ready = max(ready, in_day[len(in_day) - daily_limit] + DAY)

    return ready


def random_gap(gap_min: float, gap_max: float, rng: random.Random | None = None) -> float:
    return (rng or random).uniform(gap_min, gap_max)


def estimate_seconds(count: int, window_limit: int, window_seconds: int, gap_min: float, gap_max: float) -> float:
    """Rough ETA for `count` follows ignoring daily caps."""
    if count <= 0:
        return 0.0
    by_gap = (count - 1) * (gap_min + gap_max) / 2
    by_window = ((count - 1) // max(window_limit, 1)) * window_seconds
    return max(by_gap, by_window)
