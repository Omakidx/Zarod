import json
import traceback

import httpx
import pytest

from followbot.platforms import (
    ActionBlocked,
    Outcome,
    RateLimited,
    SessionExpired,
    TransientError,
)
from followbot.platforms.instagram import InstagramAdapter
from followbot.platforms.x import XAdapter

UA = "test-agent"


def mock(handler):
    return httpx.MockTransport(handler)


def x_adapter(handler):
    return XAdapter({"auth_token": "AT", "ct0": "CSRF", "twid": "u%3D42"}, UA, transport=mock(handler))


def ig_adapter(handler):
    return InstagramAdapter({"sessionid": "S", "csrftoken": "TOK", "ds_user_id": "42"}, UA, transport=mock(handler))


class TestX:
    async def test_verify_sends_auth_and_csrf(self):
        seen = {}

        def handler(req):
            seen.update(req.headers)
            assert req.url.path == "/i/api/1.1/account/multi/list.json"
            return httpx.Response(200, json={"users": [
                {"user_id": "7", "screen_name": "other"}, {"user_id": "42", "screen_name": "me"}]})

        assert await x_adapter(handler).verify() == "me"
        assert seen["x-csrf-token"] == "CSRF"
        assert seen["authorization"].startswith("Bearer ")
        assert "auth_token=AT" in seen["cookie"]

    async def test_verify_falls_back_to_lookup_by_own_id(self):
        def handler(req):
            if "multi/list" in req.url.path:
                return httpx.Response(404, json={"errors": [{"code": 34, "message": "gone"}]})
            assert req.url.params["user_id"] == "42"
            return httpx.Response(200, json=[{"screen_name": "me", "connections": ["none"]}])

        assert await x_adapter(handler).verify() == "me"

    async def test_verify_suspended_account(self):
        def handler(req):
            return httpx.Response(200, json={"users": [{"user_id": "42", "screen_name": "me", "is_suspended": True}]})

        with pytest.raises(ActionBlocked):
            await x_adapter(handler).verify()

    async def test_follow_new_user(self):
        def handler(req):
            if "lookup" in req.url.path:
                return httpx.Response(200, json=[{"screen_name": "bob", "connections": ["none"]}])
            assert req.method == "POST" and b"screen_name=bob" in req.content
            return httpx.Response(200, json={"id_str": "1", "protected": False})

        r = await x_adapter(handler).follow("bob")
        assert (r.outcome, r.action_sent) == (Outcome.FOLLOWED, True)

    async def test_protected_user_is_requested(self):
        def handler(req):
            if "lookup" in req.url.path:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"id_str": "1", "protected": True})

        assert (await x_adapter(handler).follow("p")).outcome == Outcome.REQUESTED

    async def test_already_following_skips_post(self):
        def handler(req):
            assert req.method == "GET", "should not POST"
            return httpx.Response(200, json=[{"screen_name": "Bob", "connections": ["following"]}])

        r = await x_adapter(handler).follow("bob")
        assert (r.outcome, r.action_sent) == (Outcome.ALREADY, False)

    @pytest.mark.parametrize(
        "status,code,expected",
        [
            (403, 161, RateLimited),
            (429, 88, RateLimited),
            (401, 32, SessionExpired),
            (403, 353, SessionExpired),
            (403, 326, ActionBlocked),
            (502, None, TransientError),
        ],
    )
    async def test_error_mapping(self, status, code, expected):
        def handler(req):
            if "lookup" in req.url.path:
                return httpx.Response(200, json=[])
            body = {"errors": [{"code": code, "message": "m"}]} if code else {}
            return httpx.Response(status, json=body)

        with pytest.raises(expected):
            await x_adapter(handler).follow("bob")

    @pytest.mark.parametrize("code,outcome", [(108, Outcome.NOT_FOUND), (63, Outcome.UNAVAILABLE), (160, Outcome.ALREADY)])
    async def test_outcome_codes(self, code, outcome):
        def handler(req):
            if "lookup" in req.url.path:
                return httpx.Response(200, json=[])
            return httpx.Response(403, json={"errors": [{"code": code, "message": "m"}]})

        assert (await x_adapter(handler).follow("bob")).outcome == outcome

    async def test_network_error_is_transient(self):
        def handler(req):
            raise httpx.ConnectError("boom")

        with pytest.raises(TransientError):
            await x_adapter(handler).follow("bob")

    async def test_network_error_does_not_chain_cookie_header(self):
        def handler(req):
            raise httpx.LocalProtocolError(f"Illegal header value {req.headers['cookie']!r}")

        with pytest.raises(TransientError) as info:
            await x_adapter(handler).follow("bob")
        text = "".join(traceback.format_exception(info.value))
        assert "auth_token=AT" not in text

    async def test_rotated_ct0_used_and_exported(self):
        calls = []

        def handler(req):
            calls.append(req.headers["x-csrf-token"])
            return httpx.Response(200, json={"users": [{"user_id": "42", "screen_name": "me"}]},
                                  headers={"set-cookie": "ct0=NEW; Domain=.x.com; Path=/"})

        a = x_adapter(handler)
        await a.verify()
        await a.verify()
        assert calls == ["CSRF", "NEW"]
        assert a.export_cookies()["ct0"] == "NEW"


def ig_profile(**user):
    return httpx.Response(200, json={"data": {"user": {"id": "777", **user}}})


HOMEPAGE = (
    '["DTSGInitialData",[],{"token":"DTSG:abc"}] ["LSD",[],{"token":"LSDTOK"}] '
    '{"actorID":"17841400000000000"} {"client_revision":1054967404} {"__spin_b":"trunk"} {"__spin_t":1794935000}'
)


def ig_site(follow_response, profile=None, homepage=HOMEPAGE, seen=None):
    """A fake instagram.com: profile lookup, homepage tokens and the GraphQL follow."""
    def handler(req):
        if seen is not None:
            seen.append(req)
        if req.url.path == "/api/v1/users/web_profile_info/":
            return profile or ig_profile()
        if req.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=homepage)
        assert req.url.path == "/api/graphql"
        return follow_response
    return handler


def gql(status):
    return httpx.Response(200, headers={"content-type": "text/javascript"},
                          text='for (;;);' + json.dumps({"data": {"xdt_create_friendship": {"friendship_status": status}}}))


class TestInstagram:
    async def test_verify(self):
        def handler(req):
            assert req.url.path == "/api/v1/accounts/edit/web_form_data/"
            assert req.headers["x-csrftoken"] == "TOK" and req.headers["x-ig-app-id"]
            return httpx.Response(200, json={"form_data": {"username": "me"}, "status": "ok"})

        assert await ig_adapter(handler).verify() == "me"

    async def test_follow_sends_the_website_mutation(self):
        seen = []
        r = await ig_adapter(ig_site(gql({"following": True}), seen=seen)).follow("bob")
        assert (r.outcome, r.action_sent) == (Outcome.FOLLOWED, True)
        post = seen[-1]
        form = dict(httpx.QueryParams(post.content.decode()))
        assert post.method == "POST" and post.headers["x-fb-friendly-name"] == "usePolarisFollowMutation"
        assert post.headers["x-fb-lsd"] == "LSDTOK" and post.headers["x-csrftoken"] == "TOK"
        assert form["doc_id"] == "26508036048874888" and form["fb_dtsg"] == "DTSG:abc" and form["lsd"] == "LSDTOK"
        assert form["av"] == "17841400000000000" and form["__rev"] == form["__spin_r"] == "1054967404"
        assert form["jazoest"] == "2" + str(sum(map(ord, "DTSG:abc")))
        assert json.loads(form["variables"]) == {
            "target_user_id": "777", "container_module": "profile",
            "nav_chain": "PolarisProfilePostsTabRoot:profilePage:1:via_cold_start",
        }

    async def test_private_account_is_requested(self):
        r = await ig_adapter(ig_site(gql({"following": False, "outgoing_request": True}))).follow("bob")
        assert (r.outcome, r.action_sent) == (Outcome.REQUESTED, True)

    async def test_tokens_are_read_once(self):
        seen = []
        adapter = ig_adapter(ig_site(gql({"following": True}), seen=seen))
        await adapter.follow("bob")
        await adapter.follow("alice")
        assert [r.url.path for r in seen].count("/") == 1

    async def test_already_following(self):
        r = await ig_adapter(lambda req: ig_profile(followed_by_viewer=True)).follow("bob")
        assert (r.outcome, r.action_sent) == (Outcome.ALREADY, False)

    async def test_not_found(self):
        assert (await ig_adapter(lambda req: httpx.Response(404)).follow("x")).outcome == Outcome.NOT_FOUND
        empty = lambda req: httpx.Response(200, json={"data": {"user": None}})
        assert (await ig_adapter(empty).follow("x")).outcome == Outcome.NOT_FOUND

    async def test_html_lookup_falls_back_to_route_resolver(self):
        seen = []
        page404 = httpx.Response(404, headers={"content-type": "text/html"}, text="<html>")
        base = ig_site(gql({"following": True}), profile=page404, seen=seen)

        def handler(req):
            if req.url.path == "/ajax/bulk-route-definitions/":
                form = dict(httpx.QueryParams(req.content.decode()))
                assert form["route_urls[0]"] == "/bob/" and form["fb_dtsg"] == "DTSG:abc"
                # The viewer's own ids appear too and must be skipped.
                return httpx.Response(200, text='for (;;);{"payload":{"payloads":{"/bob/":{"result":{"exports":'
                                                 '{"rootView":{"props":{"viewer_id":"42","profile_id":"888"}}}}}}}}')
            return base(req)

        r = await ig_adapter(handler).follow("bob")
        assert (r.outcome, r.action_sent) == (Outcome.FOLLOWED, True)
        form = dict(httpx.QueryParams(seen[-1].content.decode()))
        assert json.loads(form["variables"])["target_user_id"] == "888"

    async def test_route_resolver_error_is_not_found_and_silence_is_transient(self):
        page404 = httpx.Response(404, headers={"content-type": "text/html"}, text="<html>")

        def site(route_text):
            base = ig_site(gql({"following": True}), profile=page404)
            return lambda req: (httpx.Response(200, text=route_text)
                                if req.url.path == "/ajax/bulk-route-definitions/" else base(req))

        r = await ig_adapter(site('for (;;);{"payload":{"payloads":{"/bob/":{"error":true}}}}')).follow("bob")
        assert r.outcome == Outcome.NOT_FOUND and "unavailable" in r.detail
        with pytest.raises(TransientError, match="look up"):
            await ig_adapter(site('for (;;);{"payload":{}}')).follow("bob")

    async def test_missing_page_tokens_is_transient(self):
        with pytest.raises(TransientError, match="fb_dtsg"):
            await ig_adapter(ig_site(gql({"following": True}), homepage="<html>")).follow("bob")

    async def test_html_answer_is_transient_and_refreshes_tokens(self):
        seen = []
        adapter = ig_adapter(ig_site(httpx.Response(200, headers={"content-type": "text/html"}, text="<html>"), seen=seen))
        for _ in range(2):
            with pytest.raises(TransientError):
                await adapter.follow("bob")
        assert [r.url.path for r in seen].count("/") == 2

    @pytest.mark.parametrize(
        "message,expected",
        [("Please login to continue", SessionExpired), ("feedback_required: spam", RateLimited), ("weird", TransientError)],
    )
    async def test_graphql_errors(self, message, expected):
        resp = httpx.Response(200, json={"errors": [{"message": message}], "data": None})
        with pytest.raises(expected):
            await ig_adapter(ig_site(resp)).follow("bob")

    @pytest.mark.parametrize(
        "resp,expected",
        [
            (httpx.Response(302, headers={"location": "https://www.instagram.com/accounts/login/"}), SessionExpired),
            (httpx.Response(400, json={"message": "challenge_required"}), ActionBlocked),
            (httpx.Response(400, json={"message": "feedback_required", "spam": True}), RateLimited),
            (httpx.Response(429), RateLimited),
            (httpx.Response(403, json={"message": "login_required"}), SessionExpired),
        ],
    )
    async def test_error_mapping_on_follow(self, resp, expected):
        with pytest.raises(expected):
            await ig_adapter(ig_site(resp)).follow("bob")


class TestInstagramWire:
    async def test_claim_is_echoed(self):
        seen = []

        def handler(req):
            seen.append(req.headers["x-ig-www-claim"])
            if req.url.path == "/api/v1/users/web_profile_info/":
                return httpx.Response(200, headers={"x-ig-set-www-claim": "hmac.ABC"}, json={"data": {"user": {"id": "777"}}})
            return ig_site(gql({"following": True}))(req)

        await ig_adapter(handler).follow("bob")
        assert seen[0] == "0" and set(seen[1:]) == {"hmac.ABC"}

    @pytest.mark.parametrize(
        "ua,family",
        [
            ("Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0", "firefox"),
            ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "chrome"),
            ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0", "edge"),
        ],
    )
    def test_browser_profile_matches_user_agent(self, ua, family):
        from followbot.platforms._impersonate import browser_for

        assert browser_for(ua).startswith(family)

    async def test_curl_transport_maps_response(self, monkeypatch):
        from curl_cffi.requests import Headers

        from followbot.platforms import _impersonate

        sent = {}

        class FakeResp:
            status_code = 200
            headers = Headers([("content-type", "application/json"), ("content-encoding", "gzip"),
                               ("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/")])
            content = b'{"ok": true}'

        async def fake_request(self, method, url, **kw):
            sent.update(kw, method=method, url=url)
            return FakeResp()

        monkeypatch.setattr(_impersonate.AsyncSession, "request", fake_request)
        t = _impersonate.CurlTransport("http://vpn:8888", "firefox147")
        async with httpx.AsyncClient(transport=t, cookies={"sessionid": "S"}) as c:
            r = await c.get("https://www.instagram.com/api/v1/x/", headers={"x-test": "1"})
        names = [k.lower() for k, _ in sent["headers"]]
        assert "x-test" in names and "cookie" in names and "host" not in names and "accept-encoding" not in names
        assert sent["proxy"] == "http://vpn:8888" and sent["impersonate"] == "firefox147" and sent["allow_redirects"] is False
        assert sent["default_headers"] is False
        assert r.json() == {"ok": True} and "content-encoding" not in r.headers
        assert {c.name for c in r.cookies.jar} == {"a", "b"}
