import json

import pytest

from followbot.config import ConfigError, load_settings
from followbot.importer import import_cookie_files
from followbot.platforms import SessionExpired
from followbot.worker import vault_context

from test_worker import env  # noqa: F401  (shared fixture: db, worker, clock, notes, holder, account_id)

X_EXPORT = [{"name": "auth_token", "value": "AT", "domain": ".x.com"}, {"name": "ct0", "value": "CT", "domain": ".x.com"}]


class VerifyingAdapter:
    def __init__(self, username="tester", error=None):
        self.username, self.error, self.closed = username, error, False

    async def verify(self):
        if self.error:
            raise self.error
        return self.username

    def export_cookies(self):
        return {"auth_token": "AT", "ct0": "CT"}

    async def aclose(self):
        self.closed = True


async def run_import(env, tmp_path, adapter, content=X_EXPORT):
    db, worker, *_ = env
    (tmp_path / "x.cookies.json").write_text(content if isinstance(content, str) else json.dumps(content))
    settings = worker.settings.__class__(**{
        **worker.settings.__dict__,
        "cookie_files": {"x": str(tmp_path / "x.cookies.json")},
        "import_location": "us",
    })
    reports, proxies = [], []

    async def report(text):
        reports.append(text)

    def factory(platform, cookies, ua, proxy):
        proxies.append(proxy)
        return adapter

    await import_cookie_files(settings, db, worker.vault, worker, report, adapter_factory=factory)
    return reports, proxies


async def test_imports_once_and_skips_unchanged(env, tmp_path):
    db, worker, *_ = env
    reports, proxies = await run_import(env, tmp_path, VerifyingAdapter())
    assert reports == ["✅ Imported X @tester from secrets/x.cookies.json via 🌍 us. Tap Follow accounts to start."]
    assert proxies == ["http://vpn:8888"]
    account = next(a for a in await db.list_accounts("x") if a.username == "tester")
    assert account.proxy_name == "us"
    assert worker.vault.decrypt(vault_context("x", "tester"), account.cookies_enc)["ct0"] == "CT"

    reports, proxies = await run_import(env, tmp_path, VerifyingAdapter())
    assert reports == [] and proxies == []  # unchanged file: no request, no overwrite

    changed = X_EXPORT + [{"name": "lang", "value": "en", "domain": ".x.com"}]
    reports, _ = await run_import(env, tmp_path, VerifyingAdapter(), changed)
    assert reports[0].startswith("✅ Imported")


async def test_removed_account_is_imported_again(env, tmp_path):
    db, *_ = env
    await run_import(env, tmp_path, VerifyingAdapter())
    account = next(a for a in await db.list_accounts("x") if a.username == "tester")
    await db.delete_account(account.id)
    reports, _ = await run_import(env, tmp_path, VerifyingAdapter())
    assert reports[0].startswith("✅ Imported X @tester")


async def test_failed_check_saves_nothing_and_retries_next_start(env, tmp_path):
    db, *_ = env
    adapter = VerifyingAdapter(error=SessionExpired("X rejected the session (401)"))
    reports, _ = await run_import(env, tmp_path, adapter)
    assert "didn't work: X rejected the session" in reports[0]
    assert adapter.closed
    assert [a.username for a in await db.list_accounts("x")] == ["me"]  # only the fixture's account
    assert await db.last_import_hash("x") is None


async def test_bad_file_reported_without_contents(env, tmp_path):
    reports, proxies = await run_import(env, tmp_path, VerifyingAdapter(), '[{"name": "auth_token", "value": "SECRETVAL"}]')
    assert "Missing required cookies: ct0" in reports[0]
    assert "SECRETVAL" not in reports[0]
    assert proxies == []


def test_config_cookie_files():
    base = {"TELEGRAM_BOT_TOKEN": "t", "OWNER_TELEGRAM_ID": "1", "ENCRYPTION_KEY": "A" * 43 + "=",
            "PROXIES": "main=http://vpn:8888", "X_COOKIES_FILE": "x.cookies.json", "INSTAGRAM_COOKIES_FILE": ""}
    s = load_settings(base)
    assert isinstance(s.encryption_key, bytes) and len(s.encryption_key) == 32
    assert s.cookie_files == {"x": "/secrets/x.cookies.json"}
    assert s.import_location is None  # each imported account gets its own location
    with pytest.raises(ConfigError):
        load_settings({**base, "COOKIES_LOCATION": "elsewhere"})
