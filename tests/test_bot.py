from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from followbot.bot import BotApp, FollowFlow

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
    assert keyboard[0][0].style == "success" and keyboard[0][0].icon_custom_emoji_id


async def test_icon_fallback_resends_without_icons():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    from followbot import ui

    calls = []

    async def make_request(bot, method):
        calls.append([b.icon_custom_emoji_id for row in method.reply_markup.keyboard for b in row])
        if len(calls) == 1:
            raise TelegramBadRequest(method=method, message="Bad Request: custom emoji not allowed")
        return "ok"

    ui.state.icons = True
    try:
        method = SendMessage(chat_id=1, text="hi", reply_markup=ui.main_menu())
        assert await ui.IconFallback()(make_request, None, method) == "ok"
        assert any(calls[0]) and not any(calls[1]) and ui.state.icons is False
        assert not any(b.icon_custom_emoji_id for row in ui.main_menu().keyboard for b in row)
    finally:
        ui.state.icons = True
