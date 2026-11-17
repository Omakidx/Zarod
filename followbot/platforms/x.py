"""X (Twitter) adapter using the web client's own v1.1 endpoints and your session cookies."""

from __future__ import annotations

import time
from urllib.parse import unquote

import httpx

from . import _http
from .base import (
    ActionBlocked,
    FollowResult,
    Outcome,
    RateLimited,
    SessionExpired,
    TransientError,
)

# The public bearer token embedded in x.com's web app (identical for every visitor).
WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

_SESSION_CODES = {32, 89, 215, 353}
_BLOCKED_CODES = {64, 326}
_NOT_FOUND_CODES = {17, 34, 50, 108}
_UNAVAILABLE_CODES = {63, 162}
_DAILY_LIMIT_CODE = 161
_RATE_CODES = {88}
_ALREADY_REQUESTED_CODE = 160


class XAdapter:
    platform = "x"

    def __init__(
        self,
        cookies: dict[str, str],
        user_agent: str,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {
            "authorization": f"Bearer {WEB_BEARER}",
            "user-agent": user_agent,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "accept": "*/*",
            "origin": "https://x.com",
            "referer": "https://x.com/",
        }
        self._client = _http.make_client("https://x.com", ".x.com", cookies, headers, proxy, transport)

    def _csrf(self) -> dict[str, str]:
        return {"x-csrf-token": _http.cookie_value(self._client, "ct0") or ""}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await _http.send(self._client, method, path, headers=self._csrf(), **kwargs)
        if resp.status_code >= 500:
            raise TransientError(f"X server error {resp.status_code}")
        return resp

    def _raise_for_errors(self, resp: httpx.Response) -> None:
        """Map X's error payloads to our exceptions. Returns quietly if the response is a success."""
        body = _http.json_or_none(resp)
        errors = body.get("errors", []) if isinstance(body, dict) else []
        codes = {e.get("code") for e in errors if isinstance(e, dict)}
        message = "; ".join(str(e.get("message", "")) for e in errors if isinstance(e, dict))[:200]

        if resp.status_code == 401 or codes & _SESSION_CODES:
            raise SessionExpired(f"X rejected the session ({message or resp.status_code})")
        if codes & _BLOCKED_CODES:
            raise ActionBlocked(f"X has locked or suspended this account ({message}). Open x.com to resolve it.")
        if _DAILY_LIMIT_CODE in codes:
            raise RateLimited("X says you can't follow more people right now", retry_after=6 * 3600)
        if resp.status_code == 429 or codes & _RATE_CODES:
            reset = resp.headers.get("x-rate-limit-reset")
            wait = 900.0
            if reset and reset.isdigit():
                wait = max(60.0, float(reset) - time.time())
            raise RateLimited("X rate limit hit", retry_after=wait)

    def _own_user_id(self) -> str:
        # twid is "u=<numeric id>", URL-encoded as "u%3D<id>".
        twid = unquote(_http.cookie_value(self._client, "twid") or "")
        return twid[2:] if twid.startswith("u=") and twid[2:].isdigit() else ""

    async def verify(self) -> str:
        # account/settings.json was retired (error 34), so the web client's account switcher list is used.
        own_id = self._own_user_id()
        resp = await self._request("GET", "/i/api/1.1/account/multi/list.json")
        self._raise_for_errors(resp)
        body = _http.json_or_none(resp)
        users = body.get("users") if isinstance(body, dict) else None
        if resp.status_code == 200 and isinstance(users, list) and users:
            me = next((u for u in users if own_id and str(u.get("user_id")) == own_id), users[0])
            if me.get("is_suspended"):
                raise ActionBlocked("X has suspended this account.")
            if me.get("screen_name"):
                return me["screen_name"]

        if own_id:
            resp = await self._request("GET", "/i/api/1.1/friendships/lookup.json", params={"user_id": own_id})
            self._raise_for_errors(resp)
            body = _http.json_or_none(resp)
            if resp.status_code == 200 and isinstance(body, list) and body and body[0].get("screen_name"):
                return body[0]["screen_name"]
        raise SessionExpired(f"Couldn't read your X profile (HTTP {resp.status_code})")

    async def _connections(self, handle: str) -> list[str] | None:
        """Relationship flags for `handle`, [] if the user doesn't exist, None if lookup failed."""
        resp = await self._request("GET", "/i/api/1.1/friendships/lookup.json", params={"screen_name": handle})
        self._raise_for_errors(resp)
        body = _http.json_or_none(resp)
        if resp.status_code == 404:
            return []
        if resp.status_code != 200 or not isinstance(body, list):
            return None
        for entry in body:
            if isinstance(entry, dict) and str(entry.get("screen_name", "")).lower() == handle.lower():
                return list(entry.get("connections") or [])
        return []

    async def follow(self, handle: str) -> FollowResult:
        # An empty lookup can mean "no such user" or "suspended"; the follow call tells them apart.
        connections = await self._connections(handle) or []
        if "following" in connections:
            return FollowResult(Outcome.ALREADY, action_sent=False)
        if "following_requested" in connections:
            return FollowResult(Outcome.ALREADY, action_sent=False, detail="request already pending")

        resp = await self._request(
            "POST",
            "/i/api/1.1/friendships/create.json",
            data={"screen_name": handle, "include_profile_interstitial_type": "1", "skip_status": "1"},
        )
        self._raise_for_errors(resp)
        body = _http.json_or_none(resp)
        errors = body.get("errors", []) if isinstance(body, dict) else []
        codes = {e.get("code") for e in errors if isinstance(e, dict)}

        if resp.status_code == 200 and isinstance(body, dict) and body.get("id_str"):
            if body.get("protected"):
                return FollowResult(Outcome.REQUESTED, action_sent=True)
            return FollowResult(Outcome.FOLLOWED, action_sent=True)
        if _ALREADY_REQUESTED_CODE in codes:
            return FollowResult(Outcome.ALREADY, action_sent=True, detail="request already pending")
        if codes & _NOT_FOUND_CODES or resp.status_code == 404:
            return FollowResult(Outcome.NOT_FOUND, action_sent=True)
        if codes & _UNAVAILABLE_CODES:
            return FollowResult(Outcome.UNAVAILABLE, action_sent=True, detail="suspended or blocked")
        raise TransientError(f"Unexpected X response {resp.status_code} {sorted(c for c in codes if c)}")

    def export_cookies(self) -> dict[str, str]:
        return _http.export_cookies(self._client)

    async def aclose(self) -> None:
        await self._client.aclose()
