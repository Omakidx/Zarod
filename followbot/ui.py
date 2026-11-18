"""Buttons and the main menu: coloured buttons with icons from the "Telegram iOS Icons" pack (t.me/addemoji/tgiosicons).

Telegram only shows custom-emoji icons when the bot is allowed to use them (the bot owner has Telegram Premium,
or the bot has a Fragment username). If Telegram refuses them, IconFallback resends without icons and turns them off.
"""

from __future__ import annotations

import logging
import re

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

log = logging.getLogger(__name__)

# custom_emoji_id of each icon in the tgiosicons pack, by what it's used for.
ICONS = {
    "follow": "6033108709213736873",    # person with plus
    "accounts": "6032609071373226027",  # people
    "queue": "5766994197705921104",     # card index
    "add": "6028171274939797252",       # link
    "pause": "5891184096192763888",     # raised hand
    "resume": "5773626993010546707",    # play
    "clear": "6039522349517115015",     # bin
    "help": "6030848053177486888",      # question mark
    "instagram": "6030506650522096180",  # camera
    "x": "6030784887093464891",         # speech bubble
    "start": "5774022692642492953",     # check
    "cancel": "6030757850274336631",    # cross
    "location": "5776233299424843260",  # globe
}

GREEN, BLUE, RED = "success", "primary", "danger"


class _State:
    icons = True


state = _State()


def _icon(name: str | None) -> str | None:
    return ICONS.get(name) if name and state.icons else None


def button(text: str, data: CallbackData, icon: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack(), icon_custom_emoji_id=_icon(icon), style=style)


def inline(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Main menu: (action, label, icon, style); one inner list per row.
MENU_LAYOUT = [
    [("follow", "Follow accounts", "follow", GREEN)],
    [("accounts", "My accounts", "accounts", BLUE), ("queue", "Queue", "queue", BLUE)],
    [("add", "Add account", "add", BLUE)],
    [("pause", "Pause all", "pause", None), ("resume", "Resume all", "resume", None)],
    [("clear", "Clear queue", "clear", RED), ("help", "Help", "help", None)],
]
LABELS = {action: label for row in MENU_LAYOUT for action, label, _, _ in row}


def menu_key(text: str | None) -> str:
    """Button text without any leading emoji, lower-cased, so it matches with or without icons."""
    return re.sub(r"^[^\w]+", "", text or "").strip().lower()


MENU_ACTIONS = {menu_key(label): action for action, label in LABELS.items()}


# Plain emoji for the menu. Telegram Desktop stops sending messages in a chat whose reply keyboard has
# coloured buttons with custom-emoji icons (mobile is fine), so the menu stays plain; inline buttons keep them.
MENU_EMOJI = {"follow": "➕", "accounts": "👥", "queue": "🗂", "add": "🔗", "pause": "⏸", "resume": "▶️",
              "clear": "🗑", "help": "❓"}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"{MENU_EMOJI[action]} {label}") for action, label, _, _ in row]
            for row in MENU_LAYOUT
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an action",
    )


def _strip(markup) -> None:
    rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None) or []
    for row in rows:
        for b in row:
            b.icon_custom_emoji_id = None


class IconFallback(BaseRequestMiddleware):
    """Resend without icons if Telegram refuses custom emoji on buttons, and stop using them."""

    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            markup = getattr(method, "reply_markup", None)
            if markup is None or "emoji" not in str(exc).lower():
                raise
            if state.icons:
                log.warning("Telegram refused button icons (%s); continuing without them", exc.message)
            state.icons = False
            _strip(markup)
            return await make_request(bot, method)
