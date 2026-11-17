"""An httpx transport that sends requests through curl_cffi, so the TLS and HTTP/2 fingerprint match a real browser.

Instagram answers HTTP 429 to API calls whose connection looks like Python's, even with a valid session.
Cookies, headers and redirects stay with the httpx client; this only replaces the wire.
"""

from __future__ import annotations

import re

import httpx
from curl_cffi.requests import AsyncSession, BrowserType
from curl_cffi.requests.errors import RequestsError

# Set by httpx or curl itself; forwarding them would duplicate or contradict curl's own.
_DROP_REQUEST = {"host", "connection", "content-length", "accept-encoding", "transfer-encoding"}
# curl has already decoded the body.
_DROP_RESPONSE = {"content-encoding", "content-length", "transfer-encoding"}


def browser_for(user_agent: str) -> str:
    """Newest curl_cffi profile of the browser family named in the user agent."""
    if "Firefox/" in user_agent:
        family = "firefox"
    elif "Edg/" in user_agent:
        family = "edge"
    elif "Chrome/" in user_agent:
        family = "chrome"
    elif "Safari/" in user_agent:
        family = "safari"
    else:
        family = "chrome"
    versions = [
        (int(m.group(1)), b.value)
        for b in BrowserType
        if (m := re.fullmatch(rf"{family}(\d+)", b.value))
    ]
    return max(versions)[1] if versions else "chrome"


class CurlTransport(httpx.AsyncBaseTransport):
    def __init__(self, proxy: str | None, impersonate: str):
        self._proxy = proxy
        self._impersonate = impersonate
        self._session = AsyncSession()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in _DROP_REQUEST]
        try:
            resp = await self._session.request(
                request.method,
                str(request.url),
                headers=headers,
                data=await request.aread() or None,
                proxy=self._proxy,
                impersonate=self._impersonate,
                # Only our headers: curl's page-load defaults (sec-fetch-mode: navigate, accept: text/html)
                # make Instagram answer API calls with an HTML page.
                default_headers=False,
                allow_redirects=False,
                timeout=20,
            )
        except RequestsError as exc:
            # Only the type: curl's message can quote the request.
            if self._proxy and "proxy" in str(exc).lower():
                raise httpx.ProxyError("proxy error") from None
            raise httpx.ConnectError(f"curl error ({type(exc).__name__})") from None
        finally:
            # httpx owns the cookie jar; don't let curl keep a second copy.
            self._session.cookies.clear()
        return httpx.Response(
            resp.status_code,
            headers=[(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _DROP_RESPONSE],
            content=resp.content,
            request=request,
        )

    async def aclose(self) -> None:
        await self._session.close()
