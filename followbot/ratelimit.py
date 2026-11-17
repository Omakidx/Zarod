"""Follow budget: N follows per rolling window, a rolling 24h cap, and a random gap between follows."""

from __future__ import annotations

import random

DAY = 86_400


def next_allowed_at(
    action_times: list[float],
    now: float,
    *,
    window_limit: int,
    window_seconds: int,
    daily_limit: int,
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
