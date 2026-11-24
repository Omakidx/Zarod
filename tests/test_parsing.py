import json

import pytest

from followbot.cookies import CookieError, parse_cookie_export
from followbot.crypto import CookieVault, generate_key
from followbot.handles import MAX_HANDLES_PER_JOB, parse_handles
from followbot.ratelimit import DAY, estimate_seconds, next_allowed_at

import base64


def _export(*cookies):
    return json.dumps([{"name": n, "value": v, "domain": d, **extra} for n, v, d, extra in cookies])


class TestCookies:
    def test_cookie_editor_list_for_x(self):
        raw = _export(
            ("auth_token", "AT", ".x.com", {}),
            ("ct0", "CT", ".x.com", {"expirationDate": 9_999_999_999}),
            ("sessionid", "IG", ".instagram.com", {}),
        )
        assert parse_cookie_export(raw, "x") == {"auth_token": "AT", "ct0": "CT"}

    def test_twitter_domain_accepted(self):
        raw = _export(("auth_token", "AT", ".twitter.com", {}), ("ct0", "CT", "twitter.com", {}))
        assert parse_cookie_export(raw, "x")["ct0"] == "CT"

    def test_wrapped_and_flat_shapes(self):
        wrapped = json.dumps({"cookies": [{"name": "auth_token", "value": "A"}, {"name": "ct0", "value": "C"}]})
        assert parse_cookie_export(wrapped, "x") == {"auth_token": "A", "ct0": "C"}
        flat = json.dumps({"sessionid": "S", "csrftoken": "C", "ds_user_id": 123})
        assert parse_cookie_export(flat, "instagram") == {"sessionid": "S", "csrftoken": "C", "ds_user_id": "123"}

    def test_lookalike_domain_rejected(self):
        raw = _export(("auth_token", "A", ".evilx.com", {}), ("ct0", "C", ".evilx.com", {}))
        with pytest.raises(CookieError, match="No cookies"):
            parse_cookie_export(raw, "x")

    def test_expired_cookie_reported(self):
        raw = _export(("auth_token", "A", ".x.com", {"expirationDate": 100}), ("ct0", "C", ".x.com", {}))
        with pytest.raises(CookieError, match="expired"):
            parse_cookie_export(raw, "x", now=1_000)

    def test_missing_and_invalid(self):
        with pytest.raises(CookieError, match="ds_user_id"):
            parse_cookie_export(json.dumps({"sessionid": "S", "csrftoken": "C"}), "instagram")
        with pytest.raises(CookieError, match="valid JSON"):
            parse_cookie_export("auth_token=abc; ct0=def", "x")


class TestHandles:
    def test_mixed_input(self):
        text = "@Alice, bob\nhttps://x.com/carol_1?s=20  twitter.com/Dave https://instagram.com/eve alice"
        r = parse_handles(text, "x")
        assert r.handles == ["Alice", "bob", "carol_1", "Dave"]
        assert r.wrong_platform == ["https://instagram.com/eve"]
        assert r.duplicates == 1

    def test_instagram_rules(self):
        r = parse_handles("https://www.instagram.com/some.user/ bad..dots .lead toolong_" + "x" * 30 + " explore", "instagram")
        assert r.handles == ["some.user"]
        assert len(r.invalid) == 4

    def test_x_rejects_reserved_and_long(self):
        r = parse_handles("https://x.com/home x.com/i abcdefghijklmnop ok", "x")
        assert r.handles == ["ok"]

    def test_truncates(self):
        r = parse_handles(" ".join(f"u{i}" for i in range(MAX_HANDLES_PER_JOB + 5)), "x")
        assert len(r.handles) == MAX_HANDLES_PER_JOB and r.truncated == 5


class TestWarmup:
    def test_new_accounts_start_small_and_double_each_day(self):
        from followbot.ratelimit import DAY, warmup_caps

        caps = [warmup_caps(200, 20, d * DAY + 60) for d in range(6)]
        assert [c[0] for c in caps] == [20, 40, 80, 160, 200, 200]
        assert caps[0][1] == 3 and caps[1][1] == 5  # the day's follows spread over ~8 hours
        assert caps[4][1] is None  # fully warmed up: only the normal limits
        assert warmup_caps(200, 0, 60) == (200, None)  # warm-up off

    def test_hourly_cap(self):
        assert next_allowed_at([100, 200, 300], 1000, window_limit=13, window_seconds=600, daily_limit=20,
                               hourly_limit=3) == 100 + 3600


class TestRateLimit:
    kw = dict(window_limit=3, window_seconds=600, daily_limit=10)

    def test_free_when_under_limits(self):
        assert next_allowed_at([900, 950], 1000, **self.kw) == 1000

    def test_window_full_waits_for_oldest_to_expire(self):
        assert next_allowed_at([500, 700, 900], 1000, **self.kw) == 1100

    def test_not_before_respected(self):
        assert next_allowed_at([], 1000, not_before=1042, **self.kw) == 1042

    def test_daily_cap(self):
        now = 10 * DAY
        times = [now - DAY + 100 + i * 3600 for i in range(10)]  # 10 spread over the last day
        assert next_allowed_at(times, now, **self.kw) == times[0] + DAY

    def test_estimate(self):
        assert estimate_seconds(1, 14, 600, 25, 60) == 0
        assert estimate_seconds(29, 14, 600, 25, 60) == max(28 * 42.5, 2 * 600)


def test_vault_roundtrip_and_platform_binding():
    vault = CookieVault(base64.urlsafe_b64decode(generate_key()))
    blob = vault.encrypt("x:me", {"auth_token": "secret"})
    assert "secret" not in blob
    assert vault.decrypt("x:me", blob) == {"auth_token": "secret"}
    for wrong in ("instagram:me", "x:someone_else"):
        with pytest.raises(Exception):
            vault.decrypt(wrong, blob)


def test_unsendable_cookie_values_rejected():
    with pytest.raises(CookieError, match="corrupted"):
        parse_cookie_export(json.dumps({"auth_token": "caf\u00e9", "ct0": "C"}), "x")
    with pytest.raises(CookieError, match="number"):
        parse_cookie_export(json.dumps({"sessionid": "S", "csrftoken": "C", "ds_user_id": "../x"}), "instagram")


def test_probe_refuses_without_vpn():
    from followbot.__main__ import probe_proxy
    from followbot.config import ConfigError

    env = {"PROXIES": "us=http://vpn-us:8888"}
    with pytest.raises(ConfigError):
        probe_proxy(env, None, direct=False)
    with pytest.raises(ConfigError):
        probe_proxy(env, "de", direct=False)
    assert probe_proxy(env, "us", direct=False) == "http://vpn-us:8888"
    assert probe_proxy(env, None, direct=True) is None


def test_per_platform_user_agent():
    from followbot.config import load_settings
    from followbot.crypto import generate_key

    env = {"TELEGRAM_BOT_TOKEN": "t", "OWNER_TELEGRAM_ID": "1", "ENCRYPTION_KEY": generate_key(),
           "PROXIES": "main=http://vpn:8888", "BROWSER_USER_AGENT": "Firefox UA", "INSTAGRAM_USER_AGENT": "Brave UA"}
    s = load_settings(env)
    assert s.user_agent_for("instagram") == "Brave UA" and s.user_agent_for("x") == "Firefox UA"


def test_proxies_range_and_extra():
    from followbot.config import _parse_proxies, load_settings

    got = _parse_proxies("main=http://bot:pw@vpn:8881,ash6-ash8=http://bot:pw@vpn:8886")
    assert got == {"main": "http://bot:pw@vpn:8881", "ash6": "http://bot:pw@vpn:8886",
                   "ash7": "http://bot:pw@vpn:8887", "ash8": "http://bot:pw@vpn:8888"}
    env = {"TELEGRAM_BOT_TOKEN": "t", "OWNER_TELEGRAM_ID": "1", "ENCRYPTION_KEY": "A" * 43 + "=",
           "PROXIES": "main=http://vpn:8881", "PROXIES_EXTRA": "ash6-ash20=http://vpn:8886"}
    settings = load_settings(env)
    assert list(settings.proxies)[:2] == ["main", "ash6"] and len(settings.proxies) == 16
    assert settings.proxies["ash20"] == "http://vpn:8900"


def test_locations_file_builds_every_proxy(tmp_path):
    from followbot.config import load_settings

    f = tmp_path / "locs.txt"
    f.write_text("# comment\nmain US 1.2.3.4 KEY # Ashburn\nash2 US 1.2.3.5 KEY\nde-ber1 de 5.6.7.8 KEY2 # Berlin\n")
    env = {"TELEGRAM_BOT_TOKEN": "t", "OWNER_TELEGRAM_ID": "1", "ENCRYPTION_KEY": "A" * 43 + "=",
           "VPN_LOCATIONS_FILE": str(f), "VPN_PROXY_BASE": "http://bot:pw@vpn.railway.internal/"}
    s = load_settings(env)
    assert s.proxies == {"main": "http://bot:pw@vpn.railway.internal:8881",
                         "ash2": "http://bot:pw@vpn.railway.internal:8882",
                         "de-ber1": "http://bot:pw@vpn.railway.internal:8883"}
    assert s.location_country == {"main": "US", "ash2": "US", "de-ber1": "DE"} and s.max_per_ip == 3


def test_the_real_locations_file_parses():
    from followbot.config import LOCATIONS_FILE, load_locations

    locations = load_locations(LOCATIONS_FILE)
    assert len(locations) == 40 and locations[0] == ("main", "US") and locations[19] == ("ash20", "US")
    assert len({n for n, _ in locations}) == 40  # names are unique
