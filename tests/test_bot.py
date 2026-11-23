import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from followbot.bot import BotApp, FollowFlow, Pick

from test_worker import env  # noqa: F401


def fake_message(text=""):
    return SimpleNamespace(text=text, document=None, answer=AsyncMock(), chat=SimpleNamespace(id=1))


def make_app(db, worker):
    app = BotApp.__new__(BotApp)  # skip router setup; handlers are plain methods
    app.db, app.worker, app.settings, app.bot = db, worker, worker.settings, AsyncMock()
    return app


def fsm():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


async def test_follow_with_inline_handles_goes_straight_to_preview(env):
    db, worker, *_ = env
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args="@alice https://x.com/bob"))
    text = msg.answer.await_args.args[0]
    assert "<b>2</b> to follow" in text and "@alice, @bob" in text
    assert await state.get_state() == FollowFlow.confirm.state
    assert (await state.get_data())["handles"] == ["alice", "bob"]


async def test_follow_without_args_asks_for_handles(env):
    db, worker, *_ = env
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args=None))
    assert "Send one handle or a list" in msg.answer.await_args.args[0]
    assert await state.get_state() == FollowFlow.handles.state


async def test_invalid_inline_input_keeps_waiting_for_handles(env):
    db, worker, *_ = env
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args="!!!"))
    assert "couldn't find any valid handles" in msg.answer.await_args.args[0]
    assert await state.get_state() == FollowFlow.handles.state


def test_menu_matches_buttons_with_or_without_icons():
    from followbot import ui

    keyboard = ui.main_menu().keyboard
    labels = [b.text for row in keyboard for b in row]
    assert all(ui.menu_key(label) in ui.MENU_ACTIONS for label in labels)
    assert ui.MENU_ACTIONS[ui.menu_key("➕ Follow accounts")] == "follow"
    # Plain buttons: Telegram Desktop stops sending in a chat whose reply keyboard has icons or colours.
    assert all(b.icon_custom_emoji_id is None and b.style is None for row in keyboard for b in row)
    assert keyboard[0][0].text == "➕ Follow accounts"


async def test_icon_fallback_resends_without_icons():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    from followbot import ui

    calls = []

    async def make_request(bot, method):
        calls.append([b.icon_custom_emoji_id for row in method.reply_markup.inline_keyboard for b in row])
        if len(calls) == 1:
            raise TelegramBadRequest(method=method, message="Bad Request: custom emoji not allowed")
        return "ok"

    ui.state.icons = True
    try:
        markup = ui.inline([[ui.button("Start", Pick(kind="confirm", value="yes"), icon="start")]])
        method = SendMessage(chat_id=1, text="hi", reply_markup=markup)
        assert await ui.IconFallback()(make_request, None, method) == "ok"
        assert any(calls[0]) and not any(calls[1]) and ui.state.icons is False
        assert ui.button("Start", Pick(kind="confirm", value="yes"), icon="start").icon_custom_emoji_id is None
    finally:
        ui.state.icons = True


async def add_x_account(db, worker, name):
    return await db.upsert_account("x", name, worker.vault.encrypt(f"x:{name}", {"auth_token": "A", "ct0": "C"}), "ua", "us")


async def test_follow_uses_every_account_on_the_platform_in_one_batch(env):
    db, worker, *_ = env
    second = await add_x_account(db, worker, "two")
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    app._reported = set()
    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args="@alice @bob"))
    assert "from <b>2 X accounts</b> at the same time" in msg.answer.await_args.args[0]

    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(
        chat=SimpleNamespace(id=1), message_id=77, edit_text=AsyncMock()))
    await app.on_confirm(query, SimpleNamespace(value="yes"), state)
    jobs = await db.batch_jobs(1)
    assert sorted(j.account_id for j in jobs) == sorted([(await db.list_accounts("x"))[0].id, second])
    assert {j.batch_id for j in jobs} == {jobs[0].id} and {j.message_id for j in jobs} == {77}
    assert [await db.job_counts(j.id) for j in jobs] == [{"pending": 2}, {"pending": 2}]
    # The second account starts a few seconds after the first.
    assert (await db.get_account(second)).next_action_at > 0
    text = app.bot.edit_message_text.await_args.args[0]
    assert "X · 2 accounts" in text and "@me: 0/2" in text and "@two: 0/2" in text


async def test_paused_account_is_shown_on_preview_and_card_until_resumed(env):
    db, worker, _, _, _, acc = env
    await db.set_account_state(acc, status="paused")
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    app._reported = set()
    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args="@alice"))
    assert "@me is paused" in msg.answer.await_args.args[0]

    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(
        chat=SimpleNamespace(id=1), message_id=77, edit_text=AsyncMock()))
    await app.on_confirm(query, SimpleNamespace(value="yes"), state)
    card = app.bot.edit_message_text.await_args.args[0]
    assert "@me: 0/1 · ⏸ paused, tap Resume all" in card

    await app.cmd_resume(fake_message())
    assert (await db.get_account(acc)).status == "active"
    card = app.bot.edit_message_text.await_args.args[0]
    assert "@me: 0/1 · starting…" in card


async def test_resending_a_queued_list_does_not_queue_it_twice(env):
    db, worker, _, _, _, acc = env
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    app._reported = set()
    await db.create_job(acc, 1, ["alice", "bob"])

    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args="@alice @bob"))
    assert "All 2 are already queued" in msg.answer.await_args.args[0]
    assert await state.get_state() is None

    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args="@Alice @bob @carol"))
    text = msg.answer.await_args.args[0]
    assert "<b>1</b> to follow" in text and "2 already queued, skipped" in text
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(
        chat=SimpleNamespace(id=1), message_id=77, edit_text=AsyncMock()))
    await app.on_confirm(query, SimpleNamespace(value="yes"), state)
    assert await db.pending_handles(acc) == {"alice", "bob", "carol"}
    assert await db.pending_count(acc) == 3


async def test_own_account_is_left_out_of_its_job(env):
    db, worker, _, _, _, acc = env
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    app._reported = set()
    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args="@me @bob"))
    assert "@me: an account doesn't follow itself" in msg.answer.await_args.args[0]
    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(
        chat=SimpleNamespace(id=1), message_id=77, edit_text=AsyncMock()))
    await app.on_confirm(query, SimpleNamespace(value="yes"), state)
    assert await db.pending_handles(acc) == {"bob"}


async def test_job_card_says_when_the_next_follow_is_due(env):
    import time

    db, worker, clock, _, _, acc = env
    app = make_app(db, worker)
    clock.t = time.time()
    job = await db.create_job(acc, 1, ["alice"])
    await db.set_job_message(job, 5)
    await db.set_account_state(acc, next_action_at=clock.t + 45)
    await app._render_job(job, final=False)
    card = app.bot.edit_message_text.await_args.args[0]
    assert "@me: 0/1 · next ~" + time.strftime("%H:%M", time.localtime(clock.t + 45)) in card
    assert "one every 30–30 s per account" in card


async def test_finished_report_is_a_summary_with_grouped_problems(env):
    db, worker, *_ = env
    app = make_app(db, worker)
    app._reported = set()
    acc = (await db.list_accounts("x"))[0].id
    job = await db.create_job(acc, 1, ["a", "b", "c", "d"])
    tasks = [r["id"] for r in await (await db.conn.execute("SELECT id FROM tasks WHERE job_id = ? ORDER BY id", (job,))).fetchall()]
    for task, (status, detail) in zip(tasks, [("followed", ""), ("already", ""), ("not_found", "error route 201 chars"),
                                             ("not_found", "error route 201 chars")]):
        await db.finish_task(task, status, detail)
    await db.finish_job_if_complete(job)
    await app.report_finished(job)
    report = app.bot.send_message.await_args.args[1]
    assert "Job #%d finished" % job in report
    assert "✅ followed 1 · ↩️ already followed 1 · ❓ not found 2" in report
    assert "❓ Not found, or hidden from @me (2): @c, @d" in report
    assert "error route" not in report


async def test_a_list_split_over_two_messages_is_one_preview(env):
    db, worker, *_ = env
    app, state, msg = make_app(db, worker), fsm(), fake_message()
    first = " ".join(f"https://x.com/user{i}" for i in range(97))
    await app.cmd_follow(msg, state, CommandObject(prefix="/", command="follow", args=first))
    await app.on_handles(fake_message("https://x.com/user96 https://x.com/user97 https://x.com/user98"), state)
    assert len((await state.get_data())["handles"]) == 99
    assert await state.get_state() == FollowFlow.confirm.state


async def test_worker_runs_accounts_at_the_same_time(tmp_path):
    import asyncio

    from followbot.config import Limits, Settings
    from followbot.crypto import CookieVault, generate_key
    from followbot.db import Database
    from followbot.platforms import FollowResult, Outcome
    from followbot.worker import Worker

    import base64
    from test_worker import Notes

    db = await Database.open(str(tmp_path / "p.db"))
    vault = CookieVault(base64.urlsafe_b64decode(generate_key()))
    settings = Settings(bot_token="t", owner_id=1, encryption_key=b"0" * 32, db_path="", require_proxy=True,
                        user_agent="ua", proxies={"a": "http://vpn:8888", "b": "http://vpn2:8888"}, limits=Limits())
    started, both = [], asyncio.Event()

    class Slow:
        def __init__(self, proxy):
            self.proxy = proxy

        async def follow(self, handle):
            started.append(self.proxy)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)  # only finishes if the other account is running too
            return FollowResult(Outcome.FOLLOWED, True)

        def export_cookies(self):
            return {"auth_token": "A", "ct0": "C"}

        async def aclose(self):
            pass

    try:
        ids = [await db.upsert_account("x", n, vault.encrypt(f"x:{n}", {"auth_token": "A", "ct0": "C"}), "ua", loc)
               for n, loc in (("one", "a"), ("two", "b"))]
        batch = await db.create_job(ids[0], 1, ["target"])
        await db.create_job(ids[1], 1, ["target"], batch)
        worker = Worker(db, vault, settings, Notes(), adapter_factory=lambda p, c, ua, proxy: Slow(proxy))
        await worker.tick()
        assert sorted(started) == ["http://vpn2:8888", "http://vpn:8888"]
        assert [await db.job_counts(j.id) for j in await db.batch_jobs(batch)] == [{"followed": 1}, {"followed": 1}]
    finally:
        await db.close()


async def test_accounts_button_asks_platform_then_lists_it(env):
    db, worker, *_ = env
    await db.upsert_account("instagram", "gram", worker.vault.encrypt("instagram:gram", {"sessionid": "S"}), "ua", "us")
    app, msg = make_app(db, worker), fake_message()
    await app.cmd_accounts(msg)
    assert msg.answer.await_args.args[0] == "Which platform's accounts?"
    buttons = [b.text for row in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "Instagram · 1 account" in buttons and "X · 1 account" in buttons

    query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()))
    await app.on_accounts_platform(query, SimpleNamespace(value="instagram"))
    cards = [c.args[0] for c in query.message.answer.await_args_list]
    assert len(cards) == 1 and "@gram" in cards[0]


class _Adapter:
    """Connect-flow stand-in: verify() returns the username in the cookies, or fails if there is none."""

    def __init__(self, cookies):
        self.cookies = cookies

    async def verify(self):
        from followbot.platforms import PlatformError

        if "user" not in self.cookies:
            raise PlatformError("Instagram logged this session out")
        return self.cookies["user"]

    def export_cookies(self):
        return None

    async def aclose(self):
        pass


def _paste(user=None):
    import json

    cookies = [{"name": "sessionid", "value": "s"}, {"name": "csrftoken", "value": "c"},
               {"name": "ds_user_id", "value": "1"}]
    if user:
        cookies.append({"name": "user", "value": user})
    msg = fake_message(json.dumps(cookies))
    msg.delete = AsyncMock()
    status = SimpleNamespace(edit_text=AsyncMock())
    msg.answer = AsyncMock(return_value=status)
    return msg, status


async def test_more_accounts_can_be_pasted_without_pressing_add_account_again(env, monkeypatch):
    from followbot import bot as botmod
    from followbot.bot import ConnectFlow

    db, worker, *_ = env
    monkeypatch.setattr(botmod, "make_adapter", lambda platform, cookies, ua, proxy: _Adapter(cookies))
    app, state = make_app(db, worker), fsm()
    app.settings = dataclasses.replace(
        worker.settings, proxies={"us": "http://vpn:8888", "us2": "http://vpn2:8888", "us3": "http://vpn3:8888"})
    app.vault = worker.vault
    await state.set_state(ConnectFlow.cookies)
    await state.update_data(platform="instagram", location="us2")

    first, status = _paste("alice")
    await app.on_cookies(first, state)
    assert "Connected Instagram as <b>@alice</b> via 🌍 us2" in status.edit_text.await_args.args[0]
    assert "Paste another Instagram cookie export" in status.edit_text.await_args.args[0]
    assert await state.get_state() == ConnectFlow.cookies.state
    assert (await state.get_data())["location"] == "us3"  # the next account gets the next free IP

    rejected, status = _paste()
    await app.on_cookies(rejected, state)
    assert "Nothing was saved" in status.edit_text.await_args.args[0]
    assert await state.get_state() == ConnectFlow.cookies.state

    bad = fake_message("not json")
    bad.delete = AsyncMock()
    await app.on_cookies(bad, state)
    assert "try again" in bad.answer.await_args.args[0]
    assert await state.get_state() == ConnectFlow.cookies.state

    second, status = _paste("bob")
    await app.on_cookies(second, state)
    assert "@bob</b> via 🌍 us3" in status.edit_text.await_args.args[0]
    assert sorted(a.username for a in await db.list_accounts("instagram")) == ["alice", "bob"]


async def test_done_ends_the_add_account_step_and_keeps_the_result(env):
    from followbot.bot import ConnectFlow

    db, worker, *_ = env
    app, state = make_app(db, worker), fsm()
    await state.set_state(ConnectFlow.cookies)
    message = SimpleNamespace(edit_reply_markup=AsyncMock(), edit_text=AsyncMock(), answer=AsyncMock())
    query = SimpleNamespace(answer=AsyncMock(), message=message)
    await app.on_cancel_button(query, Pick(kind="cancel", value="done"), state)
    assert await state.get_state() is None
    message.edit_text.assert_not_awaited()
    assert message.edit_reply_markup.await_args.kwargs == {"reply_markup": None}
