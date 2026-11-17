"""Parse browser cookie-extension exports into a name -> value dict.

Accepted shapes:
  * a list of cookie objects: [{"name": ..., "value": ..., "domain": ...}, ...]
    (Cookie-Editor, EditThisCookie, and most other extensions)
  * an object wrapping that list: {"cookies": [...]}
  * a flat object: {"auth_token": "...", "ct0": "..."}
"""

from __future__ import annotations

import json
import time

MAX_EXPORT_BYTES = 512 * 1024

REQUIRED = {
    "x": ("auth_token", "ct0"),
    "instagram": ("sessionid", "csrftoken", "ds_user_id"),
}
DOMAINS = {
    "x": ("x.com", "twitter.com"),
    "instagram": ("instagram.com",),
}


class CookieError(ValueError):
    pass


def _domain_matches(domain: str, allowed: tuple[str, ...]) -> bool:
    domain = domain.lstrip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def _sendable(value: str) -> bool:
    """Printable ASCII only: anything else can't go in a Cookie header."""
    return all(0x20 < ord(c) < 0x7F for c in value)


def parse_cookie_export(raw: str | bytes, platform: str, now: float | None = None) -> dict[str, str]:
    if platform not in REQUIRED:
        raise CookieError(f"Unknown platform {platform!r}")
    if len(raw) > MAX_EXPORT_BYTES:
        raise CookieError("That export is too large to be a cookie file.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CookieError("That isn't valid JSON. Paste the export exactly as the extension copied it.") from exc

    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]

    now = time.time() if now is None else now
    cookies: dict[str, str] = {}
    expired: list[str] = []

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or "name" not in item or "value" not in item:
                continue
            domain = str(item.get("domain") or "")
            if domain and not _domain_matches(domain, DOMAINS[platform]):
                continue
            expiry = item.get("expirationDate") or item.get("expires")
            if isinstance(expiry, (int, float)) and 0 < expiry < now:
                expired.append(str(item["name"]))
                continue
            cookies[str(item["name"])] = str(item["value"])
    elif isinstance(data, dict):
        cookies = {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int))}
    else:
        raise CookieError("Expected a JSON list or object of cookies.")

    bad = [name for name in REQUIRED[platform] if name in cookies and not _sendable(cookies[name])]
    if bad:
        raise CookieError(f"These cookie values look corrupted: {', '.join(bad)}. Re-export them.")
    if platform == "instagram" and cookies.get("ds_user_id") and not cookies["ds_user_id"].isdigit():
        raise CookieError("ds_user_id should be a number. Re-export the cookies.")

    missing = [name for name in REQUIRED[platform] if not cookies.get(name)]
    if missing:
        hint = ""
        if any(name in expired for name in missing):
            hint = " Some of them have expired. Log in again in your browser and re-export."
        elif not cookies:
            hint = f" No cookies for {'/'.join(DOMAINS[platform])} were found. Did you export from the right site?"
        raise CookieError(f"Missing required cookies: {', '.join(missing)}.{hint}")
    return cookies
