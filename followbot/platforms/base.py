"""Shared contract for platform adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Outcome(StrEnum):
    FOLLOWED = "followed"          # now following
    REQUESTED = "requested"        # private/protected account: follow request pending
    ALREADY = "already"            # was already following (or already requested)
    NOT_FOUND = "not_found"        # no such user
    UNAVAILABLE = "unavailable"    # suspended, blocked you, or otherwise not followable


@dataclass
class FollowResult:
    outcome: Outcome
    # True when a follow request was actually sent; only those count against the rate budget.
    action_sent: bool
    detail: str = ""


class PlatformError(Exception):
    """Base class; `detail` is safe to show the owner (never contains cookies)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SessionExpired(PlatformError):
    """Cookies no longer work: owner must /connect again."""


class ActionBlocked(PlatformError):
    """The platform wants a human (challenge, checkpoint, locked account)."""


class RateLimited(PlatformError):
    def __init__(self, detail: str, retry_after: float):
        super().__init__(detail)
        self.retry_after = retry_after


class TransientError(PlatformError):
    """Network trouble, VPN down, 5xx: worth retrying later."""


class Adapter(Protocol):
    platform: str

    async def verify(self) -> str:
        """Return the logged-in username, or raise SessionExpired."""

    async def follow(self, handle: str) -> FollowResult: ...

    def export_cookies(self) -> dict[str, str]:
        """Current cookie values, including any the platform rotated."""

    async def aclose(self) -> None: ...
