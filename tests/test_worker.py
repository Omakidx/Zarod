import base64

import pytest

from followbot.config import Limits, Settings
from followbot.crypto import CookieVault, generate_key
from followbot.db import Database
from followbot.platforms import ActionBlocked, FollowResult, NetworkError, Outcome, RateLimited, SessionExpired, TransientError
from followbot.worker import Worker


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeAdapter:
    platform = "x"

    def __init__(self, script):
        self.script = script  # handle -> FollowResult | Exception
        self.calls = []
        self.cookies = {"auth_token": "A", "ct0": "C"}

    async def follow(self, handle):
        self.calls.append(handle)
        r = self.script.get(handle, FollowResult(Outcome.FOLLOWED, True))
        if isinstance(r, Exception):
            raise r
        return r

    def export_cookies(self):
        return self.cookies

    async def aclose(self):
        pass


class Notes:
    def __init__(self):
        self.alerts, self.progress, self.finished = [], [], []

    async def job_progress(self, job_id):
        self.progress.append(job_id)

    async def job_finished(self, job_id):
        self.finished.append(job_id)

    async def alert(self, text, *, group=None, who=None):
        self.alerts.append(text)


@pytest.fixture
async def env(tmp_path):
    db = await Database.open(str(tmp_path / "t.db"))
    vault = CookieVault(base64.urlsafe_b64decode(generate_key()))
    settings = Settings(
        bot_token="t", owner_id=1, encryption_key=b"0" * 32, db_path="", proxies={"us": "http://vpn:8888"},
        require_proxy=True, user_agent="ua",
        limits=Limits(window_limit=3, window_seconds=600, daily_limit={"x": 5, "instagram": 5}, gap_min=30, gap_max=30),
    )
    account_id = await db.upsert_account("x", "me", vault.encrypt("x:me", {"auth_token": "A", "ct0": "C"}), "ua", "us")
    clock, notes = Clock(), Notes()
    holder = {}

    def factory(platform, cookies, ua, proxy):
        assert proxy == "http://vpn:8888"
        return holder["adapter"]

    worker = Worker(db, vault, settings, notes, adapter_factory=factory, clock=clock)
    yield db, worker, clock, notes, holder, account_id
    await db.close()


async def drain(worker, clock, steps=50):
    for _ in range(steps):
        delay = await worker.tick()
        if not await worker.db.accounts_with_pending():
            return
        clock.t += delay


async def test_bulk_job_respects_window_and_gaps(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({"b": FollowResult(Outcome.ALREADY, False)})
    job = await db.create_job(acc, 99, ["a", "b", "c", "d", "e"])
    start = clock.t
    await drain(worker, clock)
    assert holder["adapter"].calls == ["a", "b", "c", "d", "e"]
    assert await db.job_counts(job) == {"followed": 4, "already": 1}
    assert notes.finished == [job]
    times = await db.action_times(acc, 0)
    assert len(times) == 4
    # 3 per 600 s: the 4th real follow can't happen before the 1st + 600 s.
    assert times[3] >= times[0] + 600
    assert all(b - a >= 30 for a, b in zip(times, times[1:]))
    assert times[0] == start


async def test_daily_cap_stops_after_limit(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({})
    await db.create_job(acc, 99, [f"u{i}" for i in range(7)])
    for _ in range(40):
        clock.t += await worker.tick()
        if len(holder["adapter"].calls) >= 5 and clock.t < 1_000_000 + 86_400 - 1000:
            break
    assert len(holder["adapter"].calls) == 5
    account = await db.get_account(acc)
    assert await worker.ready_at(account) >= 1_000_000 + 86_400


async def test_rate_limited_pauses_and_retries_same_task(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({"a": RateLimited("slow down", retry_after=3600)})
    job = await db.create_job(acc, 99, ["a"])
    await worker.tick()
    assert "Resuming automatically" in notes.alerts[0]
    assert (await db.job_counts(job)) == {"pending": 1}
    assert await worker.ready_at(await db.get_account(acc)) == clock.t + 3600
    holder["adapter"].script = {}
    clock.t += 3600
    await worker.tick()
    assert (await db.job_counts(job)) == {"followed": 1}


async def test_session_expired_holds_queue(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({"a": SessionExpired("logged out")})
    job = await db.create_job(acc, 99, ["a", "b"])
    await worker.tick()
    account = await db.get_account(acc)
    assert account.status == "expired"
    assert await worker.ready_at(account) == float("inf")
    assert (await db.job_counts(job)) == {"pending": 2}
    assert "Add account" in notes.alerts[0]


async def test_transient_retries_then_fails(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({"a": TransientError("VPN proxy unreachable")})
    job = await db.create_job(acc, 99, ["a", "b"])
    await drain(worker, clock)
    assert holder["adapter"].calls == ["a", "a", "a", "b"]
    assert (await db.job_counts(job)) == {"failed": 1, "followed": 1}


async def test_a_dead_vpn_location_holds_the_account_without_failing_follows(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({"a": NetworkError("Network error (ReadTimeout)")})
    job = await db.create_job(acc, 99, ["a", "b"])
    for _ in range(5):  # the location stays down through several retries
        clock.t += 400
        await worker.tick()
    assert holder["adapter"].calls == ["a"] * 5
    assert await db.job_counts(job) == {"pending": 2}  # nothing marked failed
    account = await db.get_account(acc)
    assert "isn't answering" in account.pause_reason and account.paused_until > clock.t
    assert len(notes.alerts) == 1  # one alert when it starts, not one per retry

    holder["adapter"].script = {}  # the location is back
    await drain(worker, clock)
    assert await db.job_counts(job) == {"followed": 2}
    assert (await db.get_account(acc)).pause_reason is None


async def test_the_platforms_own_window_is_waited_out_before_it_refuses(env):
    db, worker, clock, notes, holder, acc = env
    reset = clock.t + 600
    holder["adapter"] = FakeAdapter({"a": FollowResult(Outcome.FOLLOWED, True, resume_at=reset)})
    await db.create_job(acc, 99, ["a", "b"])
    await worker.tick()
    account = await db.get_account(acc)
    assert reset + 5 <= account.next_action_at <= reset + 30
    assert await worker.ready_at(account) >= reset
    assert notes.alerts == []  # a planned wait, not a problem


async def test_a_new_x_account_warms_up_slowly(env):
    import dataclasses

    db, worker, clock, notes, holder, acc = env
    worker.settings = dataclasses.replace(worker.settings, limits=dataclasses.replace(
        worker.settings.limits, window_limit=13, daily_limit={"x": 200, "instagram": 200},
        warmup_start={"x": 20, "instagram": 0}))
    await db.conn.execute("UPDATE accounts SET created_at = ? WHERE id = ?", (clock.t - 600, acc))
    await db.conn.commit()
    holder["adapter"] = FakeAdapter({})
    await db.create_job(acc, 99, [f"u{i}" for i in range(40)])
    start = clock.t
    for _ in range(200):
        delay = await worker.tick()
        clock.t += delay
        if clock.t - start > 6 * 3600:
            break
    sent = await db.action_times(acc, start - 1)
    assert len(sent) <= 3 * 7  # at most 3 an hour on its first day
    gaps = [b - a for a, b in zip(sorted(sent), sorted(sent)[1:])]
    assert min(gaps) >= 0.7 * 3600 / 3  # spread out, never a burst


async def test_an_established_account_is_not_slowed(env):
    import dataclasses

    db, worker, clock, notes, holder, acc = env
    worker.settings = dataclasses.replace(worker.settings, limits=dataclasses.replace(
        worker.settings.limits, warmup_start={"x": 20, "instagram": 0}))
    await db.conn.execute("UPDATE accounts SET created_at = ? WHERE id = ?", (clock.t - 30 * 86400, acc))
    await db.conn.commit()
    holder["adapter"] = FakeAdapter({})
    job = await db.create_job(acc, 99, ["a", "b"])
    await drain(worker, clock)
    assert await db.job_counts(job) == {"followed": 2}
    assert clock.t - 1_000_000 < 120


async def test_rotated_cookies_are_persisted_encrypted(env):
    db, worker, clock, notes, holder, acc = env
    adapter = FakeAdapter({})
    adapter.cookies = {"auth_token": "A", "ct0": "ROTATED"}
    holder["adapter"] = adapter
    await db.create_job(acc, 99, ["a"])
    await worker.tick()
    account = await db.get_account(acc)
    assert "ROTATED" not in account.cookies_enc
    assert worker.vault.decrypt("x:me", account.cookies_enc)["ct0"] == "ROTATED"


async def test_missing_proxy_blocks_account(env):
    db, worker, clock, notes, holder, acc = env
    await db.conn.execute("UPDATE accounts SET proxy_name = 'gone' WHERE id = ?", (acc,))
    await db.conn.commit()
    holder["adapter"] = FakeAdapter({})
    await db.create_job(acc, 99, ["a"])
    await worker.tick()
    assert (await db.get_account(acc)).status == "blocked"
    assert holder["adapter"].calls == []


async def test_a_locked_or_suspended_account_leaves_the_work_list(env):
    db, worker, clock, notes, holder, acc = env

    class Suspended(FakeAdapter):
        async def follow(self, handle):
            raise ActionBlocked("X has suspended this account.")

    holder["adapter"] = FakeAdapter({})
    other = await db.upsert_account("x", "healthy", worker.vault.encrypt("x:healthy", {"ct0": "1"}), "UA", None)
    job = await db.create_job(acc, 99, ["a", "b", "c"])
    kept = await db.create_job(other, 99, ["a"])
    holder["adapter"] = Suspended({})
    await worker.process_one(await db.get_account(acc))

    account = await db.get_account(acc)
    assert account.status == "removed" and account.cookies_enc == ""
    assert [a.username for a in await db.list_accounts()] == ["healthy"]  # gone from the panel and new jobs
    assert await db.job_counts(job) == {"dropped": 3}
    assert (await db.get_job(job)).status == "done"  # so its report goes out instead of waiting forever
    assert await db.job_counts(kept) == {"pending": 1}  # other accounts carry on
    assert notes.finished == [job] and "Add account" in notes.alerts[-1]

    # Adding it again with fresh cookies brings it back.
    await db.upsert_account("x", account.username, worker.vault.encrypt("x:me", {"ct0": "2"}), "UA", None)
    assert (await db.get_account(acc)).status == "active"


async def test_accounts_left_blocked_from_before_are_removed_but_vpn_ones_stay(env):
    db, worker, clock, notes, holder, acc = env
    vpn = await db.upsert_account("x", "novpn", worker.vault.encrypt("x:novpn", {"ct0": "1"}), "UA", None)
    job = await db.create_job(acc, 99, ["a", "b"])
    await db.set_account_state(acc, status="blocked", pause_reason="X has locked this account.")
    await db.set_account_state(vpn, status="blocked", pause_reason="VPN location 'gone' is no longer configured in PROXIES")
    assert await worker.remove_blocked_leftovers() == 1
    assert (await db.get_account(acc)).status == "removed"
    assert (await db.get_account(vpn)).status == "blocked"
    assert await db.job_counts(job) == {"dropped": 2} and notes.finished == [job]
    assert await worker.remove_blocked_leftovers() == 0  # nothing left to do on the next start


async def test_handle_followed_before_is_already_without_a_request(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({})
    await db.create_job(acc, 99, ["alice"])
    await drain(worker, clock)
    job = await db.create_job(acc, 99, ["Alice", "bob"])
    await drain(worker, clock)
    assert holder["adapter"].calls == ["alice", "bob"]
    assert await db.job_counts(job) == {"already": 1, "followed": 1}


async def test_own_handle_is_skipped_without_a_request(env):
    db, worker, clock, notes, holder, acc = env
    holder["adapter"] = FakeAdapter({})
    job = await db.create_job(acc, 99, ["Me", "bob"])
    await drain(worker, clock)
    assert holder["adapter"].calls == ["bob"]
    assert await db.job_counts(job) == {"cancelled": 1, "followed": 1}


async def test_locations_one_ip_per_account(tmp_path):
    from dataclasses import replace

    from followbot.worker import pick_location, spread_locations

    db = await Database.open(str(tmp_path / "loc.db"))
    settings = Settings(
        bot_token="t", owner_id=1, encryption_key=b"0" * 32, db_path="", require_proxy=True, user_agent="ua",
        proxies={"main": "http://vpn:8888", "ash2": "http://vpn2:8888", "ash3": "http://vpn3:8888"}, limits=Limits(),
    )
    try:
        assert pick_location(settings, []) == "main"
        # Two accounts were set up before locations existed: both on main.
        x = await db.upsert_account("x", "a", "c", "ua", "main")
        ig = await db.upsert_account("instagram", "b", "c", "ua", "main")
        assert pick_location(settings, await db.list_accounts()) == "ash2"

        moves = await spread_locations(db, settings)
        assert [(a.username, loc) for a, loc in moves] == [("b", "ash2")]
        assert (await db.get_account(x)).proxy_name == "main"  # the oldest keeps its IP
        assert (await db.get_account(ig)).proxy_name == "ash2"
        assert await spread_locations(db, settings) == []  # nothing left to move

        # Reconnecting an account keeps its location even if another one is offered.
        await db.upsert_account("instagram", "b", "c2", "ua", "ash3")
        assert (await db.get_account(ig)).proxy_name == "ash2"

        # A third account gets the last free location; a fourth shares the least-used one.
        third = pick_location(settings, await db.list_accounts())
        assert third == "ash3"
        await db.upsert_account("instagram", "c", "c", "ua", third)
        assert pick_location(settings, await db.list_accounts()) == "main"

        # An account whose location was removed from PROXIES is moved to a configured one.
        fewer = replace(settings, proxies={"main": "http://vpn:8888", "ash2": "http://vpn2:8888"})
        moves = await spread_locations(db, fewer)
        assert [(a.username, loc) for a, loc in moves] == [("c", "main")]
    finally:
        await db.close()


async def test_accounts_are_spread_evenly_over_new_locations(tmp_path):
    from collections import Counter

    from followbot.worker import spread_locations

    db = await Database.open(str(tmp_path / "spread.db"))
    five = {n: f"http://vpn:{8881 + i}" for i, n in enumerate(["main", "ash2", "ash3", "ash4", "ash5"])}
    twenty = {**five, **{f"ash{i}": f"http://vpn:{8880 + i}" for i in range(6, 21)}}
    settings = Settings(bot_token="t", owner_id=1, encryption_key=b"0" * 32, db_path="", require_proxy=True,
                        user_agent="ua", proxies=twenty, limits=Limits())
    try:
        names = list(five)
        for i in range(50):  # 50 X and 30 Instagram accounts piled on the first five locations
            await db.upsert_account("x", f"x{i}", "c", "ua", names[i % 5])
        for i in range(30):
            await db.upsert_account("instagram", f"ig{i}", "c", "ua", names[i % 5])
        moves = await spread_locations(db, settings)
        accounts = await db.list_accounts()
        assert max(Counter(a.proxy_name for a in accounts).values()) == 4  # 80 over 20
        assert max(Counter(a.proxy_name for a in accounts if a.platform == "x").values()) <= 3  # 50 X over 20
        assert len(moves) == 60 and all(loc.startswith("ash") for _, loc in moves)
        assert await spread_locations(db, settings) == []  # stable: nothing moves on the next start
    finally:
        await db.close()
