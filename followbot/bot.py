"""Telegram interface (aiogram 3). Only the owner's Telegram ID is ever answered."""

from __future__ import annotations

import html
import logging
import random
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import ui
from .config import PLATFORM_NAMES, Settings
from .cookies import CookieError, parse_cookie_export
from .crypto import CookieVault
from .db import Account, Database
from .handles import MAX_HANDLES_PER_JOB, parse_handles
from .platforms import PlatformError, make_adapter
from .ratelimit import DAY, estimate_seconds
from .worker import ProxyMissing, Worker, human_duration, pick_location, proxy_url, vault_context

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 1024 * 1024
COOKIE_MARKERS = ("auth_token", "sessionid", "ct0", "csrftoken")

# Everything is on the menu buttons; the old /commands still work but aren't advertised.
COMMANDS = [BotCommand(command="start", description="Show the menu")]

PLATFORM_ICONS = {"x": "x", "instagram": "instagram"}

# Seconds between the first follows of the accounts in one batch (randomised ±40%).
STAGGER_SECONDS = 8

STATUS_ICONS = [
    ("followed", "✅ followed"),
    ("requested", "🔒 requested"),
    ("already", "↩️ already followed"),
    ("not_found", "❓ not found"),
    ("unavailable", "🚫 unavailable"),
    ("failed", "⚠️ failed"),
    ("cancelled", "✖️ cancelled"),
]
# What the finished report lists by name, and how it explains each.
PROBLEMS = {
    "not_found": "❓ Not found, or hidden from {who}",
    "unavailable": "🚫 Can't be followed by {who} (blocked or suspended)",
    "failed": "⚠️ Failed for {who} after retries",
}
# Why an account's queued follows aren't moving, shown on the job card.
HELD = {"paused": "⏸ paused, tap Resume all", "blocked": "⛔ blocked, tap Resume all",
        "expired": "🔑 needs new cookies (Add account)"}


class ConnectFlow(StatesGroup):
    cookies = State()


class FollowFlow(StatesGroup):
    handles = State()
    confirm = State()


class Pick(CallbackData, prefix="p"):
    kind: str          # platform | location | fplat | aplat | confirm | acct | cancel
    value: str


def _cancel() -> tuple:
    return ("Cancel", Pick(kind="cancel", value="x"), "cancel", ui.RED)


def _kb(rows: list[list[tuple]]) -> InlineKeyboardMarkup:
    """Rows of (text, callback data[, icon name[, style]]) tuples."""
    return ui.inline([[ui.button(*b) for b in row] for row in rows])



def _label(account: Account) -> str:
    return f"{PLATFORM_NAMES[account.platform]} @{html.escape(account.username)}"


def _cookie_help(platform: str) -> str:
    needed = "auth_token, ct0" if platform == "x" else "sessionid, csrftoken, ds_user_id"
    site = "x.com" if platform == "x" else "instagram.com"
    return (
        f"Send your <b>{PLATFORM_NAMES[platform]}</b> cookie export as JSON.\n\n"
        f"1. With Surfshark on Ashburn, log in at {site} in your browser.\n"
        f"2. Export the cookies with your extension (JSON).\n"
        f"3. Paste them here, or send them as a <code>.json</code> file. Use a file if the export is long, "
        f"because Telegram splits long messages.\n\n"
        f"Needed: <code>{needed}</code>. I delete your message as soon as I've read it.\n\n"
        f"<i>Adding a second account? Don't press Log out on the first one in your browser, because that ends "
        f"its session here too. Use {site}'s Switch account → Add account, or a private window.</i>"
    )


class BotApp:
    def __init__(self, bot: Bot, db: Database, vault: CookieVault, settings: Settings, worker: Worker):
        self.bot = bot
        self.db = db
        self.vault = vault
        self.settings = settings
        self.worker = worker
        self.router = Router(name="followbot")
        self._reported: set[int] = set()  # batches whose final report was sent
        # Owner only, and only in the private chat (never echo cookies into a group).
        owner = F.from_user.id == settings.owner_id
        self.router.message.filter(owner, F.chat.type == "private")
        self.router.callback_query.filter(owner, F.message.chat.type == "private")
        self._register()

    # ---- Notifier (used by the worker) ----

    async def alert(self, text: str) -> None:
        # Alerts carry platform error text; never let it be parsed as HTML.
        await self.bot.send_message(self.settings.owner_id, html.escape(text))

    async def job_progress(self, job_id: int) -> None:
        await self._render_job(job_id, final=False)

    async def job_finished(self, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            return
        jobs = await self.db.batch_jobs(job.batch_id)
        if any(j.status == "queued" for j in jobs):
            await self._render_job(job_id, final=False)  # other accounts are still going
            return
        if job.batch_id in self._reported:
            return
        self._reported.add(job.batch_id)
        await self._render_job(job_id, final=True)
        totals: dict[str, int] = {}
        sections = []
        for j in jobs:
            for status, n in (await self.db.job_counts(j.id)).items():
                totals[status] = totals.get(status, 0) + n
            account = await self.db.get_account(j.account_id)
            who = f"@{html.escape(account.username)}" if account else "a removed account"
            problems = await self.db.job_task_details(j.id, tuple(PROBLEMS))
            for status, heading in PROBLEMS.items():
                rows = [(h, d) for h, st, d in problems if st == status]
                if not rows:
                    continue
                handles = ", ".join("@" + html.escape(h) for h, _ in rows[:40])
                if len(rows) > 40:
                    handles += f", … +{len(rows) - 40}"
                sections.append(f"{heading.format(who=who)} ({len(rows)}): {handles}")
                if status == "failed":
                    sections.append(f"   last error: {html.escape(rows[-1][1][:120])}")
        summary = " · ".join(f"{label} {totals[st]}" for st, label in STATUS_ICONS if totals.get(st))
        lines = [f"🏁 <b>Job #{job.batch_id} finished</b>", summary or "Nothing to do."]
        if sections:
            lines += [""] + sections
        await self.bot.send_message(job.chat_id, "\n".join(lines))

    async def _render_job(self, job_id: int, final: bool) -> None:
        """One live message per batch: a line per account."""
        job = await self.db.get_job(job_id)
        if job is None or job.message_id is None:
            return
        jobs = await self.db.batch_jobs(job.batch_id)
        total = left = 0
        rows = []
        platform = None
        for j in jobs:
            account = await self.db.get_account(j.account_id)
            counts = await self.db.job_counts(j.id)
            n, pending = sum(counts.values()), counts.get("pending", 0)
            total, left = total + n, left + pending
            parts = [f"{label} {counts[s]}" for s, label in STATUS_ICONS if counts.get(s)]
            name = f"@{html.escape(account.username)}" if account else "removed account"
            platform = platform or (account.platform if account else None)
            waiting = "starting…"
            if pending and account and account.status in HELD:
                waiting = HELD[account.status]
                parts.append(waiting)
            elif pending and account and not final:
                ready = await self.worker.ready_at(account)
                if ready - time.time() > 5:
                    parts.append("next ~" + time.strftime("%H:%M", time.localtime(ready)))
            rows.append(f"{name}: {n - pending}/{n} · " + (" · ".join(parts) or waiting))
        head = f"{'🏁' if final else '🔄'} <b>Job #{job.batch_id}</b>"
        if platform:
            head += f" · {PLATFORM_NAMES[platform]} · {len(jobs)} account{'s' if len(jobs) != 1 else ''}"
        text = head + "\n" + "\n".join(rows)
        if left:
            gaps = self.settings.limits
            text += f"\n⏳ {left} left · one every {gaps.gap_min:.0f}–{gaps.gap_max:.0f} s per account"
        try:
            await self.bot.edit_message_text(text, chat_id=job.chat_id, message_id=job.message_id)
        except TelegramBadRequest:
            pass  # message unchanged or deleted

    # ---- handlers ----

    def _register(self) -> None:
        r = self.router
        # Menu buttons work from any step: they end whatever flow was open.
        r.message(F.text.func(lambda t: ui.menu_key(t) in ui.MENU_ACTIONS))(self.on_menu)
        r.message(CommandStart())(self.cmd_start)
        r.message(Command("help"))(self.cmd_start)
        r.message(Command("cancel"))(self.cmd_cancel)
        r.message(Command("connect"))(self.cmd_connect)
        r.message(Command("follow"))(self.cmd_follow)
        r.message(Command("accounts"))(self.cmd_accounts)
        r.message(Command("queue"))(self.cmd_queue)
        r.message(Command("pause"))(self.cmd_pause)
        r.message(Command("resume"))(self.cmd_resume)
        r.message(Command("clearqueue"))(self.cmd_clearqueue)
        r.callback_query(Pick.filter(F.kind == "platform"))(self.on_platform)
        r.callback_query(Pick.filter(F.kind == "location"))(self.on_location)
        r.callback_query(Pick.filter(F.kind == "fplat"))(self.on_follow_platform)
        r.callback_query(Pick.filter(F.kind == "aplat"))(self.on_accounts_platform)
        r.callback_query(Pick.filter(F.kind == "confirm"))(self.on_confirm)
        r.callback_query(Pick.filter(F.kind == "acct"))(self.on_account_action)
        r.callback_query(Pick.filter(F.kind == "cancel"))(self.on_cancel_button)
        r.message(StateFilter(ConnectFlow.cookies))(self.on_cookies)
        r.message(StateFilter(FollowFlow.handles, FollowFlow.confirm))(self.on_handles)
        r.message()(self.on_stray)

    async def cmd_start(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        limits = self.settings.limits
        await message.answer(
            "👋 <b>Follow bot</b>\n\n"
            "➕ <b>Follow accounts</b>: send handles, @handles, profile links, or a .txt/.csv file\n"
            "🔗 <b>Add account</b>: connect another X or Instagram account with its cookie JSON\n"
            "👥 <b>My accounts</b>: pause or remove an account\n"
            "🗂 <b>Queue</b>: what's waiting and when the next follow goes out\n\n"
            f"Limits: {limits.window_limit} follows per {limits.window_seconds // 60} min per account, "
            f"spaced randomly, plus a daily cap (X {limits.daily_limit['x']}, "
            f"Instagram {limits.daily_limit['instagram']}).",
            reply_markup=ui.main_menu(),
        )

    async def on_menu(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        action = ui.MENU_ACTIONS[ui.menu_key(message.text)]
        if action == "follow":
            await self.cmd_follow(message, state)
        elif action == "accounts":
            await self.cmd_accounts(message)
        elif action == "queue":
            await self.cmd_queue(message)
        elif action == "add":
            await self.cmd_connect(message, state)
        elif action == "pause":
            await self.cmd_pause(message)
        elif action == "resume":
            await self.cmd_resume(message)
        elif action == "clear":
            await self.cmd_clearqueue(message)
        else:
            await self.cmd_start(message, state)

    async def on_cancel_button(self, query: CallbackQuery, state: FSMContext) -> None:
        await query.answer()
        await state.clear()
        await query.message.edit_text("Cancelled.")

    async def cmd_cancel(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Cancelled.", reply_markup=ui.main_menu())

    # -- connect --

    async def cmd_connect(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Which platform is the account on?",
            reply_markup=_kb([
                [("Instagram", Pick(kind="platform", value="instagram"), "instagram", ui.BLUE),
                 ("X", Pick(kind="platform", value="x"), "x", ui.BLUE)],
                [_cancel()],
            ]),
        )

    async def on_platform(self, query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
        await query.answer()
        await state.update_data(platform=callback_data.value)
        # Each new account gets a VPN location (an Ashburn IP) of its own; an account you
        # reconnect keeps the one it already has.
        await self._ask_cookies(query.message, state, pick_location(self.settings, await self.db.list_accounts()))

    async def on_location(self, query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
        await query.answer()
        if callback_data.value not in self.settings.proxies:
            await query.message.edit_text("That location is no longer configured. Tap <b>Add account</b> again.")
            return
        await self._ask_cookies(query.message, state, callback_data.value)

    async def _ask_cookies(self, message: Message, state: FSMContext, location: str | None) -> None:
        await state.update_data(location=location)
        await state.set_state(ConnectFlow.cookies)
        platform = (await state.get_data())["platform"]
        where = (f"\n🌍 A new account gets its own IP: <b>{html.escape(location)}</b>. "
                 "One you reconnect keeps its current one.") if location else ""
        await message.edit_text(_cookie_help(platform) + where, reply_markup=_kb([[_cancel()]]))

    async def on_cookies(self, message: Message, state: FSMContext) -> None:
        document, text = message.document, message.text
        # Delete first so the cookies leave the chat even if anything below fails.
        await self._delete(message)
        raw: bytes | str | None = text
        if document and (document.file_size or 0) <= MAX_UPLOAD_BYTES:
            try:
                raw = (await self.bot.download(document)).read()
            except Exception:
                log.warning("cookie file download failed")
                raw = None
        if not raw:
            await message.answer("Send the cookie JSON as text or as a .json file.", reply_markup=_kb([[_cancel()]]))
            return

        data = await state.get_data()
        platform, location = data["platform"], data.get("location")
        try:
            cookies = parse_cookie_export(raw, platform)
        except CookieError as exc:
            hint = ""
            if text and len(text) >= 4000:
                hint = "\nThat looks like Telegram cut it off. Send it as a .json file instead."
            await message.answer(f"❌ {html.escape(str(exc))}{hint}")
            return

        status = await message.answer("Checking the session through the VPN…")
        try:
            adapter = make_adapter(platform, cookies, self.settings.user_agent_for(platform), proxy_url(self.settings, location))
        except ProxyMissing as exc:
            await status.edit_text(f"❌ {html.escape(str(exc))}")
            return
        try:
            username = await adapter.verify()
            cookies = adapter.export_cookies() or cookies
        except PlatformError as exc:
            await status.edit_text(f"❌ {html.escape(exc.detail)}\nNothing was saved. Tap <b>Add account</b> to try again.")
            return
        except Exception as exc:
            log.warning("session check failed: %s", type(exc).__name__)
            await status.edit_text("❌ Couldn't check that session. Nothing was saved. Tap <b>Add account</b> to try again.")
            return
        finally:
            await adapter.aclose()

        account_id = await self.db.upsert_account(
            platform, username, self.vault.encrypt(vault_context(platform, username), cookies),
            self.settings.user_agent_for(platform), location
        )
        await self.worker.forget(account_id)
        self.worker.wake()
        await state.clear()
        saved = await self.db.get_account(account_id)
        location = saved.proxy_name if saved else location
        where = f" via 🌍 {html.escape(location)}" if location else ""
        await status.edit_text(f"✅ Connected {PLATFORM_NAMES[platform]} as <b>@{html.escape(username)}</b>{where}.\nTap <b>Follow accounts</b> to start.")

    async def _delete(self, message: Message) -> None:
        try:
            await message.delete()
        except TelegramBadRequest:
            await message.answer("⚠️ I couldn't delete your message. Please delete it yourself.")

    async def on_stray(self, message: Message) -> None:
        """Anything outside a flow. Cookie-looking pastes (late fragments, after a restart) get deleted."""
        blob = (message.text or "") + (message.document.file_name or "" if message.document else "")
        if message.document or any(marker in blob for marker in COOKIE_MARKERS):
            await self._delete(message)
            await message.answer("🧹 Deleted that message because it may contain cookies. To connect an account, tap <b>Add account</b>.", reply_markup=ui.main_menu())
            return
        await message.answer("Use the menu buttons below.", reply_markup=ui.main_menu())

    # -- follow --

    async def _usable(self, platform: str | None = None) -> list[Account]:
        """Accounts that can take follows (paused ones queue until resumed), optionally on one platform."""
        return [a for a in await self.db.list_accounts(platform) if a.status in ("active", "paused")]

    async def _platform_picker(self, message: Message, kind: str, prompt: str, accounts: list[Account]) -> None:
        counts = {p: sum(a.platform == p for a in accounts) for p in PLATFORM_NAMES}
        rows = [[(f"{PLATFORM_NAMES[p]} · {n} account{'s' if n != 1 else ''}", Pick(kind=kind, value=p),
                  PLATFORM_ICONS[p], ui.BLUE)] for p, n in counts.items() if n]
        rows.append([_cancel()])
        await message.answer(prompt, reply_markup=_kb(rows))

    async def cmd_follow(self, message: Message, state: FSMContext, command: CommandObject | None = None) -> None:
        """Every connected account on the chosen platform follows the list. `/follow @a @b` skips the handles step."""
        await state.clear()
        inline = ((command.args if command else None) or "").strip()
        accounts = await self._usable()
        if not accounts:
            await message.answer("No usable accounts yet. Tap <b>Add account</b> first.")
            return
        platforms = {a.platform for a in accounts}
        if len(platforms) == 1:
            await self._start_follow(message, state, platforms.pop(), inline, edit=False)
            return
        await state.update_data(pending_text=inline)
        await self._platform_picker(message, "fplat", "Follow on which platform? All its accounts will follow.", accounts)

    async def on_follow_platform(self, query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
        await query.answer()
        pending = (await state.get_data()).get("pending_text") or ""
        await self._start_follow(query.message, state, callback_data.value, pending, edit=True)

    async def _start_follow(self, message: Message, state: FSMContext, platform: str, text: str, edit: bool) -> None:
        accounts = await self._usable(platform)
        if not accounts:
            await message.answer(f"No usable {PLATFORM_NAMES[platform]} accounts. Tap <b>Add account</b> first.")
            return
        await state.update_data(platform=platform, pending_text=None)
        who = ", ".join(f"@{html.escape(a.username)}" for a in accounts)
        head = f"Following from all {len(accounts)} {PLATFORM_NAMES[platform]} account{'s' if len(accounts) != 1 else ''}: {who}"
        if text:
            await (message.edit_text(head) if edit else message.answer(head))
            await self._preview(message, state, platform, text)
            return
        await state.set_state(FollowFlow.handles)
        prompt = (
            f"{head}.\n\n"
            "Send one handle or a list: <code>@name</code>, <code>name</code>, or profile links, "
            "separated by spaces, commas, or new lines. "
            f"Up to {MAX_HANDLES_PER_JOB} per job.\n\n"
            "Long list? Telegram may hold a message with lots of links. Send the names without links, "
            "a .txt/.csv file, or several messages (they add up)."
        )
        markup = _kb([[_cancel()]])
        await (message.edit_text(prompt, reply_markup=markup) if edit else message.answer(prompt, reply_markup=markup))

    async def on_handles(self, message: Message, state: FSMContext) -> None:
        if message.document:
            if (message.document.file_size or 0) > MAX_UPLOAD_BYTES:
                await message.answer("That file is too large (max 1 MB).")
                return
            text = (await self.bot.download(message.document)).read().decode("utf-8", errors="replace")
        else:
            text = message.text or ""
        data = await state.get_data()
        platform = data.get("platform")
        if platform is None:
            await state.clear()
            await message.answer("That step expired. Tap <b>Follow accounts</b> again.")
            return
        if await state.get_state() == FollowFlow.confirm.state and data.get("handles"):
            # Telegram splits a long list into several messages: each one adds to the open preview.
            text = "\n".join(data["handles"]) + "\n" + text
        await self._preview(message, state, platform, text)

    async def _preview(self, message: Message, state: FSMContext, platform: str, text: str) -> None:
        await state.set_state(FollowFlow.handles)  # so a retry after bad input is still handled
        accounts = await self._usable(platform)
        parsed = parse_handles(text, platform)
        if not parsed.handles:
            await message.answer("I couldn't find any valid handles in that. Try again.", reply_markup=_kb([[_cancel()]]))
            return

        # A handle every account already has queued (say, the same list sent twice) is not queued again.
        queued_sets = [await self.db.pending_handles(a.id) for a in accounts]
        already = [h for h in parsed.handles if queued_sets and all(h.lower() in q for q in queued_sets)]
        parsed.handles = [h for h in parsed.handles if h not in already]
        if not parsed.handles:
            await state.clear()
            await message.answer(f"All {len(already)} are already queued. The job card shows their progress.")
            return

        limits = self.settings.limits
        daily = limits.daily_limit[platform]
        # Accounts run side by side, so the slowest one (longest queue) sets the time.
        longest, over_cap = 0, []
        for account in accounts:
            queued = await self.db.pending_count(account.id)
            longest = max(longest, queued)
            used_today = len(await self.db.action_times(account.id, time.time() - DAY))
            if used_today + queued + len(parsed.handles) > daily:
                over_cap.append(f"@{html.escape(account.username)} ({used_today}/{daily} used)")
        eta = estimate_seconds(longest + len(parsed.handles), limits.window_limit, limits.window_seconds,
                               limits.gap_min, limits.gap_max)

        n = len(accounts)
        lines = [f"<b>{len(parsed.handles)}</b> to follow, from <b>{n} {PLATFORM_NAMES[platform]} "
                 f"account{'s' if n != 1 else ''}</b> at the same time"]
        preview = ", ".join("@" + html.escape(h) for h in parsed.handles[:15])
        if len(parsed.handles) > 15:
            preview += f", … +{len(parsed.handles) - 15}"
        lines.append(preview)
        if parsed.duplicates:
            lines.append(f"• {parsed.duplicates} duplicates removed")
        own = [a.username for a in accounts if a.username.lower() in {h.lower() for h in parsed.handles}]
        if own:
            names = ", ".join("@" + html.escape(u) for u in own)
            lines.append(f"• {names}: an account doesn't follow itself, skipped for it")
        if already:
            lines.append(f"• {len(already)} already queued, skipped")
        if parsed.wrong_platform:
            lines.append(f"• {len(parsed.wrong_platform)} links for the other platform skipped")
        if parsed.invalid:
            shown = ", ".join(html.escape(t[:40]) for t in parsed.invalid[:5])
            lines.append(f"• {len(parsed.invalid)} invalid skipped: {shown}")
        if parsed.truncated:
            lines.append(f"• {parsed.truncated} over the {MAX_HANDLES_PER_JOB} limit dropped")
        if longest:
            lines.append(f"• up to {longest} already queued ahead of these")
        lines.append(f"⏱ about {human_duration(eta)} (each account keeps its own limits)")
        held = [a for a in accounts if a.status == "paused"]
        if held:
            names = ", ".join(f"@{html.escape(a.username)}" for a in held)
            lines.append(f"⏸ {names} {'is' if len(held) == 1 else 'are'} paused: "
                         "these wait until you tap <b>Resume all</b>.")
        lines.append("Sending more handles now adds them to this list.")
        if over_cap:
            lines.append("⚠️ Goes past today's cap for " + ", ".join(over_cap) + ". The rest waits for the cap to free up.")

        await state.update_data(handles=parsed.handles)
        await state.set_state(FollowFlow.confirm)
        await message.answer(
            "\n".join(lines),
            reply_markup=_kb([[("Start", Pick(kind="confirm", value="yes"), "start", ui.GREEN),
                               ("Cancel", Pick(kind="confirm", value="no"), "cancel", ui.RED)]]),
        )

    async def on_confirm(self, query: CallbackQuery, callback_data: Pick, state: FSMContext) -> None:
        await query.answer()
        data = await state.get_data()
        await state.clear()
        if callback_data.value != "yes":
            await query.message.edit_text("Cancelled.")
            return
        accounts = await self._usable(data.get("platform"))
        if not data.get("handles") or not data.get("platform"):
            # The preview's state lives in memory, so it is lost when the bot restarts.
            await query.message.edit_text("This preview has expired. Tap <b>Follow accounts</b> again.")
            return
        if not accounts:
            await query.message.edit_text("No usable accounts left on that platform.")
            return
        batch_id = None
        now = time.time()
        for i, account in enumerate(accounts):
            queued = await self.db.pending_handles(account.id)
            handles = [h for h in data["handles"] if h.lower() not in queued and h.lower() != account.username.lower()]
            if not handles:
                continue
            job_id = await self.db.create_job(account.id, query.message.chat.id, handles, batch_id)
            batch_id = batch_id or job_id
            await self.db.set_job_message(job_id, query.message.message_id)
            if i:
                # A few seconds apart, so several accounts don't hit the same profile in the same second.
                start = now + STAGGER_SECONDS * i * random.uniform(0.6, 1.4)
                await self.db.set_account_state(account.id, next_action_at=max(account.next_action_at, start))
        if batch_id is None:
            await query.message.edit_text("All of these are already queued.")
            return
        await self._render_job(batch_id, final=False)
        self.worker.wake()

    # -- accounts / queue --

    async def _account_line(self, account: Account) -> str:
        now = time.time()
        used = len(await self.db.action_times(account.id, now - DAY))
        queued = await self.db.pending_count(account.id)
        state = {"active": "🟢 active", "paused": "⏸ paused", "expired": "🔑 needs new cookies (Add account)",
                 "blocked": "⛔ blocked"}.get(account.status, account.status)
        if account.status == "active" and account.paused_until > now:
            state = f"⏸ cooling down {human_duration(account.paused_until - now)}"
        where = f" · 🌍 {html.escape(account.proxy_name)}" if account.proxy_name else ""
        line = (f"<b>{_label(account)}</b>{where}\n{state} · today {used}/"
                f"{self.settings.limits.daily_limit[account.platform]} · queued {queued}")
        if account.pause_reason and (account.status != "active" or account.paused_until > now):
            line += f"\n<i>{html.escape(account.pause_reason or '')}</i>"
        return line

    async def cmd_accounts(self, message: Message) -> None:
        accounts = await self.db.list_accounts()
        if not accounts:
            await message.answer("No accounts yet. Tap <b>Add account</b>.")
            return
        if len({a.platform for a in accounts}) > 1:
            await self._platform_picker(message, "aplat", "Which platform's accounts?", accounts)
            return
        await self._show_accounts(message, accounts)

    async def on_accounts_platform(self, query: CallbackQuery, callback_data: Pick) -> None:
        await query.answer()
        accounts = await self.db.list_accounts(callback_data.value)
        n = len(accounts)
        await query.message.edit_text(f"<b>{PLATFORM_NAMES[callback_data.value]}</b>: {n} account{'s' if n != 1 else ''}")
        await self._show_accounts(query.message, accounts)

    async def _show_accounts(self, message: Message, accounts: list[Account]) -> None:
        if not accounts:
            await message.answer("No accounts on that platform. Tap <b>Add account</b>.")
            return
        for account in accounts:
            toggle = (("Resume", "resume", "resume", ui.GREEN) if account.status != "active"
                      else ("Pause", "pause", "pause", None))
            await message.answer(
                await self._account_line(account),
                reply_markup=_kb([[(toggle[0], Pick(kind="acct", value=f"{toggle[1]}.{account.id}"), toggle[2], toggle[3]),
                                   ("Remove", Pick(kind="acct", value=f"rm.{account.id}"), "clear", ui.RED)]]),
            )

    async def on_account_action(self, query: CallbackQuery, callback_data: Pick) -> None:
        await query.answer()
        action, _, raw_id = callback_data.value.partition(".")
        account = await self.db.get_account(int(raw_id))
        if account is None:
            await query.message.edit_text("That account is already gone.")
            return
        if action == "pause":
            await self.db.set_account_state(account.id, status="paused")
        elif action == "resume":
            if account.status == "expired":
                await query.message.answer("This session expired. Tap <b>Add account</b> with fresh cookies instead.")
                return
            await self.db.set_account_state(account.id, status="active")
            self.worker.wake()
        elif action == "rm":
            await query.message.edit_text(
                f"Remove {_label(account)}? Its stored cookies and queue are deleted.",
                reply_markup=_kb([[("Yes, remove", Pick(kind="acct", value=f"rmyes.{account.id}"), "clear", ui.RED),
                                   ("Keep", Pick(kind="cancel", value="keep"), "cancel", None)]]),
            )
            return
        elif action == "rmyes":
            await self.worker.forget(account.id)
            await self.db.delete_account(account.id)
            again = " Restart the bot to import it again from its cookie file, or" if (
                account.platform in self.settings.cookie_files) else ""
            await query.message.edit_text(f"Removed {_label(account)}.{again} tap <b>Add account</b> to add it back.")
            return
        account = await self.db.get_account(account.id)
        await query.message.edit_text(await self._account_line(account))

    async def cmd_queue(self, message: Message) -> None:
        lines = []
        for account in await self.db.list_accounts():
            if await self.db.pending_count(account.id) == 0:
                continue
            line = await self._account_line(account)
            ready = await self.worker.ready_at(account)
            if ready != float("inf"):
                wait = ready - time.time()
                line += "\nnext follow: " + ("now" if wait <= 1 else f"in {human_duration(wait)}")
            lines.append(line)
        await message.answer("\n\n".join(lines) if lines else "Nothing queued.")

    async def cmd_pause(self, message: Message) -> None:
        for account in await self.db.list_accounts():
            if account.status == "active":
                await self.db.set_account_state(account.id, status="paused")
        await message.answer("⏸ All accounts paused. Tap <b>Resume all</b> to continue.")

    async def cmd_resume(self, message: Message) -> None:
        resumed, expired = 0, []
        for account in await self.db.list_accounts():
            if account.status in ("paused", "blocked"):
                await self.db.set_account_state(account.id, status="active")
                resumed += 1
            elif account.status == "expired":
                expired.append(_label(account))
        self.worker.wake()
        for batch_id in await self.db.open_batches():
            await self._render_job(batch_id, final=False)  # drop the "paused" note from live job cards
        text = f"▶️ Resumed {resumed} account(s)."
        if expired:
            text += "\nStill need fresh cookies (<b>Add account</b>): " + ", ".join(expired)
        await message.answer(text)

    async def cmd_clearqueue(self, message: Message) -> None:
        jobs = await self.db.cancel_pending()
        await message.answer(f"Cleared. {jobs} job(s) cancelled.")


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(COMMANDS)
