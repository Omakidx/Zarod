"""Turn free-form user input (handles, @handles, profile links) into clean usernames."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SPLIT = re.compile(r"[\s,;]+")
_URL = re.compile(
    r"^(?:https?://)?(?:www\.|mobile\.|m\.)?"
    r"(?P<host>x\.com|twitter\.com|instagram\.com)/(?P<name>[^/?#]+)",
    re.IGNORECASE,
)
_HOST_PLATFORM = {"x.com": "x", "twitter.com": "x", "instagram.com": "instagram"}
_VALID = {
    "x": re.compile(r"^[A-Za-z0-9_]{1,15}$"),
    "instagram": re.compile(r"^(?!.*\.\.)(?!\.)(?!.*\.$)[A-Za-z0-9._]{1,30}$"),
}
_RESERVED = {
    "x": {"home", "explore", "i", "search", "notifications", "messages", "settings", "intent", "share", "compose", "login", "signup", "tos", "privacy"},
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "direct", "tv", "about", "legal", "developer"},
}

MAX_HANDLES_PER_JOB = 500


@dataclass
class ParseResult:
    handles: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    wrong_platform: list[str] = field(default_factory=list)
    duplicates: int = 0
    truncated: int = 0


def parse_handles(text: str, platform: str) -> ParseResult:
    result = ParseResult()
    seen: set[str] = set()
    for token in filter(None, _SPLIT.split(text)):
        name = token
        match = _URL.match(token)
        if match:
            if _HOST_PLATFORM[match.group("host").lower()] != platform:
                result.wrong_platform.append(token)
                continue
            name = match.group("name")
        name = name.lstrip("@")
        if not _VALID[platform].match(name) or name.lower() in _RESERVED[platform]:
            result.invalid.append(token)
            continue
        key = name.lower()
        if key in seen:
            result.duplicates += 1
            continue
        seen.add(key)
        result.handles.append(name)

    if len(result.handles) > MAX_HANDLES_PER_JOB:
        result.truncated = len(result.handles) - MAX_HANDLES_PER_JOB
        del result.handles[MAX_HANDLES_PER_JOB:]
    return result
