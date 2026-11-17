"""Background worker: drains each account's follow queue within its rate budget."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter
from typing import Callable, Protocol

from .config import Settings
from .crypto import CookieVault
from .db import Account, Database
from .platforms import (
    ActionBlocked,
    Adapter,
    RateLimited,
    SessionExpired,
    TransientError,
    make_adapter,
)
from .ratelimit import DAY, next_allowed_at, random_gap

log = logging.getLogger(__name__)

IDLE_POLL = 300.0
LOOKUP_GAP = (3.0, 8.0)  # pause after a task that only looked a user up


class Notifier(Protocol):
    async def job_progress(self, job_id: int) -> None: ...
    async def job_finished(self, job_id: int) -> None: ...
    async def alert(self, text: str) -> None: ...


def vault_context(platform: str, username: str) -> str:
    return f"{platform}:{username.lower()}"


class ProxyMissing(Exception):
    pass


def pick_location(settings: Settings, accounts: list) -> str | None:
    """A VPN location for a new account: the first one no account uses yet, else the least used.

    Each location is one Ashburn server with its own exit IP, so accounts don't share an IP
    until there are more accounts than locations.
    """
    if not settings.proxies:
        return None
    used = Counter(a.proxy_name for a in accounts)
    return min(settings.proxies, key=lambda name: (used[name], list(settings.proxies).index(name)))


async def spread_locations(db, settings: Settings) -> list[tuple]:
    """Give accounts that share a location (or have a missing one) a location of their own.

    The oldest account on each location keeps it. Returns (account, new location) for each move.
    """
    accounts = sorted(await db.list_accounts(), key=lambda a: a.id)
    kept, moves = [], []
    for account in accounts:
        taken = {a.proxy_name for a in kept}
        if account.proxy_name in settings.proxies and account.proxy_name not in taken:
            kept.append(account)
            continue
        free = [name for name in settings.proxies if name not in taken]
        if not free and account.proxy_name in settings.proxies:
            kept.append(account)  # more accounts than locations: leave it where it is
            continue
        new = free[0] if free else pick_location(settings, kept)
        if new and new != account.proxy_name:
            await db.set_account_location(account.id, new)
            moves.append((account, new))
            account.proxy_name = new
        kept.append(account)
    return moves


def proxy_url(settings: Settings, proxy_name: str | None) -> str | None:
    if proxy_name:
        url = settings.proxies.get(proxy_name)
        if url:
            return url
        if settings.require_proxy:
            raise ProxyMissing(f"VPN location '{proxy_name}' is no longer configured in PROXIES")
        return None
    if settings.require_proxy:
        raise ProxyMissing("This account has no VPN location and REQUIRE_PROXY is on")
    return None


AdapterFactory = Callable[[str, dict[str, str], str, str | None], Adapter]


class Worker:
    def __init__(
        self,
        db: Database,
        vault: CookieVault,
        settings: Settings,
        notifier: Notifier,
        adapter_factory: AdapterFactory = make_adapter,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.vault = vault
        self.settings = settings
        self.notifier = notifier
        self.adapter_factory = adapter_factory
        self.clock = clock
        self.rng = rng or random.Random()
        self._adapters: dict[int, tuple[str, Adapter]] = {}
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def forget(self, account_id: int) -> None:
        cached = self._adapters.pop(account_id, None)
        if cached:
            await cached[1].aclose()

    async def close(self) -> None:
        for account_id in list(self._adapters):
            await self.forget(account_id)

    async def run(self) -> None:
        while True:
            try:
                delay = await self.tick()
            except Exception:
                log.exception("worker tick failed")
                delay = 30.0
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def ready_at(self, account: Account) -> float:
        if account.status != "active":
            return float("inf")
        now = self.clock()
        limits = self.settings.limits
        return next_allowed_at(
            await self.db.action_times(account.id, now - DAY),
            now,
            window_limit=limits.window_limit,
            window_seconds=limits.window_seconds,
            daily_limit=limits.daily_limit[account.platform],
            not_before=max(account.next_action_at, account.paused_until),
        )

    async def tick(self) -> float:
        """Run every task that is due right now; return seconds until the next one may be."""
        accounts = [a for a in [await self.db.get_account(i) for i in await self.db.accounts_with_pending()] if a]
        due = [a for a in accounts if await self.ready_at(a) <= self.clock()]
        # Accounts are independent (own cookies, own IP, own limits), so the due ones run at the same time.
        results = await asyncio.gather(*(self.process_one(a) for a in due), return_exceptions=True)
        for account, result in zip(due, results):
            if isinstance(result, BaseException):
                log.error("follow for account %s failed: %s", account.id, type(result).__name__)
        soonest = IDLE_POLL
        for account in accounts:
            account = await self.db.get_account(account.id)
            if account is not None:
                soonest = min(soonest, await self.ready_at(account) - self.clock())
        return max(1.0, soonest)

    async def _adapter(self, account: Account) -> Adapter:
        cached = self._adapters.get(account.id)
        if cached and cached[0] == account.cookies_enc:
            return cached[1]
        await self.forget(account.id)
        cookies = self.vault.decrypt(vault_context(account.platform, account.username), account.cookies_enc)
        adapter = self.adapter_factory(
            account.platform, cookies, account.user_agent, proxy_url(self.settings, account.proxy_name)
        )
        self._adapters[account.id] = (account.cookies_enc, adapter)
        return adapter

    async def _save_rotated_cookies(self, account: Account, adapter: Adapter) -> None:
        current = adapter.export_cookies()
        context = vault_context(account.platform, account.username)
        if current and current != self.vault.decrypt(context, account.cookies_enc):
            blob = self.vault.encrypt(context, current)
            await self.db.update_cookies(account.id, blob)
            self._adapters[account.id] = (blob, adapter)

    async def process_one(self, account: Account) -> None:
        task = await self.db.next_task(account.id)
        if task is None:
            return
        if await self.db.followed_before(account.id, task.handle):
            # Instagram's follow call answers "following" either way, so it can't tell us this itself.
            await self.db.finish_task(task.id, "already", "followed earlier by this bot")
            await self._after_task(task.job_id)
            return
        name = f"@{account.username} ({account.platform})"
        now = self.clock()

        try:
            adapter = await self._adapter(account)
            result = await adapter.follow(task.handle)
        except ProxyMissing as exc:
            await self.db.set_account_state(account.id, status="blocked", pause_reason=str(exc))
            await self.notifier.alert(f"⛔ {name} paused: {exc}. Fix PROXIES, then tap Resume all.")
            return
        except SessionExpired as exc:
            await self.forget(account.id)
            await self.db.set_account_state(account.id, status="expired", pause_reason=exc.detail)
            await self.notifier.alert(f"🔑 {name}: {exc.detail}. Its queue is on hold. Tap Add account with fresh cookies.")
            return
        except ActionBlocked as exc:
            await self.forget(account.id)
            await self.db.set_account_state(account.id, status="blocked", pause_reason=exc.detail)
            await self.notifier.alert(f"⛔ {name}: {exc.detail} Then tap Resume all.")
            return
        except RateLimited as exc:
            until = now + exc.retry_after
            await self.db.set_account_state(account.id, paused_until=until, pause_reason=exc.detail)
            await self.notifier.alert(
                f"⏸ {name}: {exc.detail}. Resuming automatically in about {human_duration(exc.retry_after)}."
            )
            return
        except Exception as exc:  # TransientError and anything unexpected
            if not isinstance(exc, TransientError):
                log.exception("unexpected error following %s", task.handle)
            detail = exc.detail if isinstance(exc, TransientError) else f"internal error: {type(exc).__name__}"
            attempts = await self.db.bump_attempts(task.id, detail)
            if attempts >= self.settings.limits.max_attempts:
                await self.db.finish_task(task.id, "failed", detail)
                await self._after_task(task.job_id)
            await self.db.set_account_state(account.id, next_action_at=now + 60 * attempts)
            return

        await self.db.finish_task(task.id, result.outcome.value, result.detail)
        if result.action_sent:
            await self.db.record_action(account.id, now)
            gap = random_gap(self.settings.limits.gap_min, self.settings.limits.gap_max, self.rng)
        else:
            gap = self.rng.uniform(*LOOKUP_GAP)
        await self.db.set_account_state(account.id, next_action_at=now + gap, pause_reason=None, clear_reason=True)
        await self._save_rotated_cookies(account, adapter)
        await self._after_task(task.job_id)

    async def _after_task(self, job_id: int) -> None:
        if await self.db.finish_job_if_complete(job_id):
            await self.notifier.job_finished(job_id)
        else:
            await self.notifier.job_progress(job_id)


def human_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"
