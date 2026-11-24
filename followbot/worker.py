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
    NetworkError,
    RateLimited,
    SessionExpired,
    TransientError,
    make_adapter,
)
from .ratelimit import DAY, HOUR, next_allowed_at, random_gap, warmup_caps

log = logging.getLogger(__name__)

IDLE_POLL = 300.0
LOOKUP_GAP = (3.0, 8.0)  # pause after a task that only looked a user up
LOCATION_RETRY = 300.0   # wait this long when the account's VPN location doesn't answer


class Notifier(Protocol):
    """In production the Outbox: it only queues, so these return at once and don't raise."""

    async def job_progress(self, job_id: int) -> None: ...
    async def job_finished(self, job_id: int) -> None: ...
    async def alert(self, text: str, *, group: str | None = None, who: str | None = None) -> None: ...


def account_caps(settings: Settings, account: Account, now: float) -> tuple[int, int | None]:
    """This account's (daily cap, hourly cap or None), with warm-up for recently added accounts."""
    limits = settings.limits
    return warmup_caps(limits.daily_limit[account.platform], limits.warmup_start.get(account.platform, 0),
                       now - account.created_at)


def vault_context(platform: str, username: str) -> str:
    return f"{platform}:{username.lower()}"


class ProxyMissing(Exception):
    pass


def pick_location(settings: Settings, accounts: list, exclude: tuple | list = ()) -> str | None:
    """A VPN location for a new account: the first one no account uses yet, else the least used.

    Each location is one Ashburn server with its own exit IP, so accounts don't share an IP
    until there are more accounts than locations.
    """
    names = [name for name in settings.proxies if name not in exclude]
    if not names:
        return None
    used = Counter(a.proxy_name for a in accounts)
    return min(names, key=lambda name: (used[name], names.index(name)))


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
        daily, hourly = account_caps(self.settings, account, now)
        return next_allowed_at(
            await self.db.action_times(account.id, now - DAY),
            now,
            window_limit=limits.window_limit,
            window_seconds=limits.window_seconds,
            daily_limit=daily,
            hourly_limit=hourly,
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
                # The type alone hid what went wrong (an OperationalError said nothing). Database and
                # Telegram messages carry no cookies; anything else still logs only its type.
                said = f": {result}" if type(result).__module__.split(".")[0] in ("sqlite3", "aiogram") else ""
                log.error("follow for account %s failed: %s%s", account.id, type(result).__name__, said)
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
        if task.handle.lower() == account.username.lower():
            # An account can't follow itself; Instagram's lookup hides its own id, so this would only retry.
            await self.db.finish_task(task.id, "cancelled", "this is the account itself")
            await self._after_task(task.job_id)
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
            await self._alert(f"⛔ {name} paused: {exc}. Fix PROXIES, then tap Resume all.",
                              group=f"⛔ Paused: {exc}. Fix PROXIES, then tap Resume all.", who=name)
            return
        except SessionExpired as exc:
            await self.forget(account.id)
            await self.db.set_account_state(account.id, status="expired", pause_reason=exc.detail)
            await self._alert(f"🔑 {name}: {exc.detail}. Its queue is on hold. Tap Add account with fresh cookies.",
                              group=f"🔑 {exc.detail}. Their queues are on hold. Tap Add account with fresh cookies.",
                              who=name)
            return
        except ActionBlocked as exc:
            # Locked, suspended or challenged: it can't follow until a human fixes it, so it leaves
            # the work list instead of holding its queue (and every job card) hostage.
            await self._remove(account, exc.detail)
            return
        except RateLimited as exc:
            until = now + exc.retry_after
            await self.db.set_account_state(account.id, paused_until=until, pause_reason=exc.detail)
            await self._alert(
                f"⏸ {name}: {exc.detail}. Resuming automatically in about {human_duration(exc.retry_after)}.",
                group=f"⏸ {exc.detail}. Resuming automatically in about {human_duration(exc.retry_after)}.",
                who=name,
            )
            return
        except NetworkError as exc:
            # The VPN location didn't answer: not this profile's fault, so the follow keeps its retries
            # and the account just waits. One alert when it starts, not one every retry.
            where = account.proxy_name or "direct"
            reason = f"VPN location {where} isn't answering ({exc.detail})"
            await self.forget(account.id)
            await self.db.set_account_state(account.id, paused_until=now + LOCATION_RETRY, pause_reason=reason)
            if not (account.pause_reason or "").startswith(f"VPN location {where} isn't answering"):
                await self._alert(
                    f"🌐 {name}: {reason}. Its follows wait and retry every {human_duration(LOCATION_RETRY)}.",
                    group=f"🌐 VPN location {where} isn't answering. These accounts wait and retry every "
                          f"{human_duration(LOCATION_RETRY)}; check the vpn service if it lasts.",
                    who=name,
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
            _, hourly = account_caps(self.settings, account, now)
            if hourly:
                # Warming up: spread the hour's follows over the hour instead of bunching them.
                gap = max(gap, self.rng.uniform(0.7, 1.3) * HOUR / hourly)
        else:
            gap = self.rng.uniform(*LOOKUP_GAP)
        next_at = now + gap
        if result.resume_at:
            # The platform's own window is nearly used up: wait for its reset (plus a little jitter).
            next_at = max(next_at, result.resume_at + self.rng.uniform(5, 30))
        await self.db.set_account_state(account.id, next_action_at=next_at, pause_reason=None, clear_reason=True)
        await self._save_rotated_cookies(account, adapter)
        await self._after_task(task.job_id)

    async def remove_blocked_leftovers(self) -> int:
        """Remove accounts still marked blocked by the platform from before locked accounts were
        removed automatically. Ones held only for a missing VPN location stay: that's a settings fix.
        Returns how many were removed."""
        removed = 0
        for account in await self.db.list_accounts():
            if account.status != "blocked" or "VPN location" in (account.pause_reason or ""):
                continue
            await self._remove(account, account.pause_reason or "Locked or suspended by the platform.")
            removed += 1
        return removed

    async def _remove(self, account: Account, why: str) -> None:
        await self.forget(account.id)
        jobs = await self.db.remove_account(account.id, why)
        name = f"@{account.username} ({account.platform})"
        fix = ("Removed from your accounts and its queued follows dropped. "
               "Once it's fixed, tap Add account with fresh cookies to use it again.")
        await self._alert(f"🗑 {name}: {why} {fix}", group=f"🗑 {why} {fix}", who=name)
        for job_id in jobs:
            await self._after_task(job_id)

    async def _after_task(self, job_id: int) -> None:
        finished = await self.db.finish_job_if_complete(job_id)
        await self._notify(self.notifier.job_finished if finished else self.notifier.job_progress, job_id)

    async def _alert(self, text: str, *, group: str, who: str) -> None:
        await self._notify(self.notifier.alert, text, group=group, who=who)

    async def _notify(self, send, *args, **kwargs) -> None:
        """A notification must never undo or interrupt a follow that already happened."""
        try:
            await send(*args, **kwargs)
        except Exception as exc:
            log.warning("notification failed: %s", type(exc).__name__)


def human_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"
