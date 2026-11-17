"""Instagram adapter using instagram.com's web endpoints and your browser session cookies."""

from __future__ import annotations

import json
import re

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

# The app id instagram.com's web client sends on every API call (public, same for everyone).
WEB_APP_ID = "936619743392459"
WEB_ASBD_ID = "359341"

# A normal page load, as when you open instagram.com in the browser.
_PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
}

# The website follows people with this GraphQL mutation (captured from Brave, 2026-11-17).
# If Instagram changes it, the follow fails with a GraphQL error; capture it again with tools/ig-capture.
FOLLOW_DOC_ID = "26508036048874888"
FOLLOW_FRIENDLY_NAME = "usePolarisFollowMutation"
FOLLOW_NAV_CHAIN = "PolarisProfilePostsTabRoot:profilePage:1:via_cold_start"

# Page tokens the website sends with every GraphQL call, read from the homepage.
_TOKEN_PATTERNS = {
    "fb_dtsg": [r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', r'"dtsg":\{"token":"([^"]+)"', r'name="fb_dtsg" value="([^"]+)"'],
    "lsd": [r'"LSD",\[\],\{"token":"([^"]+)"', r'name="lsd" value="([^"]+)"'],
    "av": [r'"actorID":"(\d+)"', r'"viewerID":"(\d+)"'],
    "__rev": [r'"client_revision":(\d+)'],
    "__spin_b": [r'"__spin_b":"([^"]+)"'],
    "__spin_t": [r'"__spin_t":(\d+)'],
    "__hs": [r'"haste_session":"([^"]+)"'],
    "__hsi": [r'"hsi":"(\d+)"'],
    "routing_namespace": [r'"routing_namespace":"([^"]+)"', r'"(igx_www\$[^"]+)"'],
}
_REQUIRED_TOKENS = ("fb_dtsg", "lsd", "av")
# Where a route definition names the profile's account id, most specific first.
_ROUTE_ID_PATTERNS = [r'"profile_id":"(\d+)"', r'"user_id":"(\d+)"', r'"target_id":"(\d+)"', r'"id":"(\d+)"']


def _first(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        if m := re.search(pattern, text):
            return m.group(1)
    return None


def _find_status(node) -> dict | None:
    """The friendship_status-like object anywhere in a GraphQL response."""
    if isinstance(node, dict):
        if isinstance(node.get("following"), bool) or isinstance(node.get("outgoing_request"), bool):
            return node
        for value in node.values():
            if (found := _find_status(value)) is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            if (found := _find_status(value)) is not None:
                return found
    return None


class InstagramAdapter:
    platform = "instagram"

    def __init__(
        self,
        cookies: dict[str, str],
        user_agent: str,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {
            "user-agent": user_agent,
            "x-ig-app-id": WEB_APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "x-asbd-id": WEB_ASBD_ID,
            "x-ig-www-claim": "0",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.5",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "origin": "https://www.instagram.com",
            "referer": "https://www.instagram.com/",
        }
        self._user_id = cookies.get("ds_user_id", "")
        self._tokens: dict[str, str] | None = None
        if transport is None:
            from ._impersonate import CurlTransport, browser_for

            transport = CurlTransport(proxy, browser_for(user_agent))
        self._client = _http.make_client(
            "https://www.instagram.com", ".instagram.com", cookies, headers, proxy, transport
        )

    async def _request(
        self, method: str, path: str, *, referer: str | None = None, extra: dict | None = None, **kwargs
    ) -> httpx.Response:
        headers = {"x-csrftoken": _http.cookie_value(self._client, "csrftoken") or ""}
        if referer:
            headers["referer"] = referer
        headers.update(extra or {})
        resp = await _http.send(self._client, method, path, headers=headers, **kwargs)
        # The web client echoes back the claim Instagram hands it on later calls.
        if claim := resp.headers.get("x-ig-set-www-claim"):
            self._client.headers["x-ig-www-claim"] = claim
        self._raise_for_errors(resp)
        return resp

    @staticmethod
    def _raise_for_errors(resp: httpx.Response) -> None:
        location = resp.headers.get("location", "")
        if resp.is_redirect and ("/accounts/login" in location or "/challenge" in location):
            if "/challenge" in location:
                raise ActionBlocked("Instagram wants you to confirm it's you. Open the app or instagram.com.")
            raise SessionExpired("Instagram logged this session out")

        body = _http.json_or_none(resp)
        message = str(body.get("message", "")) if isinstance(body, dict) else ""
        lowered = message.lower()

        if "login_required" in lowered or resp.status_code == 401:
            raise SessionExpired("Instagram logged this session out")
        if "challenge_required" in lowered or "checkpoint_required" in lowered:
            raise ActionBlocked("Instagram wants you to confirm it's you. Open the app or instagram.com.")
        if "feedback_required" in lowered or (isinstance(body, dict) and body.get("spam")):
            raise RateLimited("Instagram temporarily blocked follows on this account", retry_after=12 * 3600)
        if resp.status_code == 429 or "please wait a few minutes" in lowered:
            raise RateLimited("Instagram rate limit hit", retry_after=3600)
        if resp.status_code >= 500:
            raise TransientError(f"Instagram server error {resp.status_code}")

    async def verify(self) -> str:
        # The website's own settings page call; users/{id}/info/ is answered with 429 for web sessions.
        resp = await self._request(
            "GET", "/api/v1/accounts/edit/web_form_data/", referer="https://www.instagram.com/accounts/edit/"
        )
        body = _http.json_or_none(resp)
        form = body.get("form_data") if isinstance(body, dict) else None
        if resp.status_code == 200 and isinstance(form, dict) and form.get("username"):
            return form["username"]
        raise SessionExpired(f"Couldn't read your Instagram profile (HTTP {resp.status_code})")

    async def follow(self, handle: str) -> FollowResult:
        resp = await self._request(
            "GET",
            "/api/v1/users/web_profile_info/",
            params={"username": handle},
            referer=f"https://www.instagram.com/{handle}/",
        )
        body = _http.json_or_none(resp)
        shape = f"HTTP {resp.status_code} {resp.headers.get('content-type', '').split(';')[0]}"
        if body is None and "html" in shape:
            # This endpoint sometimes answers with a 404 page even for profiles that open fine in the browser,
            # so ask the way the website resolves a profile link instead.
            return await self._follow_via_route(handle)
        user = (body.get("data") or {}).get("user") if isinstance(body, dict) else None
        if resp.status_code == 404 or (resp.status_code == 200 and not user):
            return FollowResult(Outcome.NOT_FOUND, action_sent=False, detail=shape)
        if resp.status_code != 200 or not isinstance(user, dict) or not user.get("id"):
            raise TransientError(f"Unexpected Instagram profile response {shape}")

        if user.get("followed_by_viewer"):
            return FollowResult(Outcome.ALREADY, action_sent=False)
        if user.get("requested_by_viewer"):
            return FollowResult(Outcome.ALREADY, action_sent=False, detail="request already pending")
        if user.get("blocked_by_viewer") or user.get("has_blocked_viewer"):
            return FollowResult(Outcome.UNAVAILABLE, action_sent=False, detail="blocked")

        return await self._follow_mutation(handle, str(user["id"]))

    def _common_form(self, tokens: dict[str, str]) -> dict[str, str]:
        form = {
            "__d": "www",
            "__user": "0",
            "__a": "1",
            "__req": "1",
            "__comet_req": "7",
            "fb_dtsg": tokens["fb_dtsg"],
            # Facebook's form checksum: "2" followed by the sum of fb_dtsg's character codes.
            "jazoest": "2" + str(sum(ord(ch) for ch in tokens["fb_dtsg"])),
            "lsd": tokens["lsd"],
            **{k: tokens[k] for k in ("__rev", "__spin_b", "__spin_t", "__hs", "__hsi") if k in tokens},
        }
        if "__rev" in tokens:
            form["__spin_r"] = tokens["__rev"]
        return form

    async def _id_from_route(self, handle: str) -> tuple[str | None, str]:
        """The account id from the website's profile-link resolver, plus a note on what came back."""
        tokens = await self._page_tokens()
        data = {**self._common_form(tokens), "route_urls[0]": f"/{handle}/"}
        if "routing_namespace" in tokens:
            data["routing_namespace"] = tokens["routing_namespace"]
        resp = await self._request(
            "POST", "/ajax/bulk-route-definitions/", data=data, referer=f"https://www.instagram.com/{handle}/",
            extra={"x-fb-lsd": tokens["lsd"]},
        )
        text = resp.text.removeprefix("for (;;);")
        own = {self._user_id, tokens.get("av", "")}
        for pattern in _ROUTE_ID_PATTERNS:
            for found in re.findall(pattern, text):
                if found not in own:
                    return found, "ok"
        not_found = '"error":true' in text or "PolarisErrorRoot" in text or "httpErrorPage" in text
        return None, f"{self._note(resp)}{' error route' if not_found else ''} {len(text)} chars"

    async def _follow_via_route(self, handle: str) -> FollowResult:
        user_id, note = await self._id_from_route(handle)
        if user_id is None:
            if "error route" in note:
                return FollowResult(Outcome.NOT_FOUND, action_sent=False, detail=f"profile unavailable ({note})")
            raise TransientError(f"Couldn't look up @{handle} on Instagram ({note})")
        # No following/requested flags on this path; the mutation's answer says which it was.
        return await self._follow_mutation(handle, user_id)

    async def _page_tokens(self) -> dict[str, str]:
        if self._tokens is None:
            resp = await self._request("GET", "/", extra=_PAGE_HEADERS)
            text = resp.text if resp.status_code == 200 else ""
            tokens = {k: v for k, patterns in _TOKEN_PATTERNS.items() if (v := _first(patterns, text))}
            missing = [k for k in _REQUIRED_TOKENS if k not in tokens]
            if missing:
                raise TransientError(f"Couldn't read Instagram page tokens ({', '.join(missing)}; HTTP {resp.status_code})")
            self._tokens = tokens
        return self._tokens

    async def _follow_mutation(self, handle: str, user_id: str) -> FollowResult:
        tokens = await self._page_tokens()
        variables = {"target_user_id": user_id, "container_module": "profile", "nav_chain": FOLLOW_NAV_CHAIN}
        data = {
            "av": tokens["av"],
            **self._common_form(tokens),
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": FOLLOW_FRIENDLY_NAME,
            "variables": json.dumps(variables, separators=(",", ":")),
            "server_timestamps": "true",
            "doc_id": FOLLOW_DOC_ID,
        }
        resp = await self._request(
            "POST",
            "/api/graphql",
            data=data,
            referer=f"https://www.instagram.com/{handle}/",
            extra={"x-fb-friendly-name": FOLLOW_FRIENDLY_NAME, "x-fb-lsd": tokens["lsd"]},
        )
        text = resp.text.removeprefix("for (;;);")
        try:
            body = json.loads(text)
        except ValueError:
            self._tokens = None  # stale tokens are the usual cause; read them again on the retry
            raise TransientError(f"Instagram didn't accept the follow ({self._note(resp)})") from None

        status = _find_status(body.get("data") if isinstance(body, dict) else None)
        if status is not None:
            if status.get("following"):
                return FollowResult(Outcome.FOLLOWED, action_sent=True)
            if status.get("outgoing_request"):
                return FollowResult(Outcome.REQUESTED, action_sent=True)

        errors = body.get("errors") if isinstance(body, dict) else None
        message = "; ".join(str(e.get("message", "")) for e in errors if isinstance(e, dict))[:160] if errors else ""
        lowered = message.lower()
        if "login" in lowered:
            raise SessionExpired("Instagram logged this session out")
        if "spam" in lowered or "feedback" in lowered or "try again later" in lowered:
            raise RateLimited("Instagram temporarily blocked follows on this account", retry_after=12 * 3600)
        self._tokens = None
        keys = ",".join(sorted(body)) if isinstance(body, dict) else type(body).__name__
        raise TransientError(f"Unexpected Instagram follow response [{keys}] {message}".strip())

    @staticmethod
    def _note(resp: httpx.Response) -> str:
        return f"{resp.status_code} {resp.headers.get('content-type', '').split(';')[0]}"

    def export_cookies(self) -> dict[str, str]:
        return _http.export_cookies(self._client)

    async def aclose(self) -> None:
        await self._client.aclose()
