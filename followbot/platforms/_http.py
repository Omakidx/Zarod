from __future__ import annotations

import httpx

from .base import NetworkError

TIMEOUT = httpx.Timeout(20.0, connect=15.0)


def make_client(
    base_url: str,
    cookie_domain: str,
    cookies: dict[str, str],
    headers: dict[str, str],
    proxy: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    jar = httpx.Cookies()
    for name, value in cookies.items():
        jar.set(name, value, domain=cookie_domain, path="/")
    return httpx.AsyncClient(
        base_url=base_url,
        cookies=jar,
        headers=headers,
        proxy=proxy if transport is None else None,
        transport=transport,
        timeout=TIMEOUT,
        follow_redirects=False,
    )


def cookie_value(client: httpx.AsyncClient, name: str) -> str | None:
    # Several cookies with the same name can exist after the server rotates one; take the last.
    value = None
    for cookie in client.cookies.jar:
        if cookie.name == name:
            value = cookie.value
    return value


def export_cookies(client: httpx.AsyncClient) -> dict[str, str]:
    return {cookie.name: cookie.value for cookie in client.cookies.jar if cookie.value is not None}


async def send(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return await client.request(method, url, **kwargs)
    except httpx.ProxyError as exc:
        raise NetworkError("VPN proxy unreachable") from None
    except httpx.TransportError as exc:
        # `from None`: the original exception can embed the Cookie header.
        # Only our own curl message is shown: httpx's messages can quote request headers.
        said = f": {exc}" if str(exc).startswith("curl error") else ""
        raise NetworkError(f"Network error ({type(exc).__name__}{said})") from None


def json_or_none(resp: httpx.Response) -> dict | list | None:
    try:
        return resp.json()
    except ValueError:
        return None
