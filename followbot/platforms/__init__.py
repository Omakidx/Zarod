from __future__ import annotations

from .base import (
    ActionBlocked,
    Adapter,
    FollowResult,
    Outcome,
    PlatformError,
    RateLimited,
    SessionExpired,
    TransientError,
)
from .instagram import InstagramAdapter
from .x import XAdapter

__all__ = [
    "ActionBlocked",
    "Adapter",
    "FollowResult",
    "Outcome",
    "PlatformError",
    "RateLimited",
    "SessionExpired",
    "TransientError",
    "make_adapter",
]


def make_adapter(platform: str, cookies: dict[str, str], user_agent: str, proxy: str | None) -> Adapter:
    if platform == "x":
        return XAdapter(cookies, user_agent, proxy)
    if platform == "instagram":
        return InstagramAdapter(cookies, user_agent, proxy)
    raise ValueError(f"Unknown platform {platform!r}")
