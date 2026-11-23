"""Delivers the worker's notifications to Telegram without ever slowing or breaking the worker.

The worker only queues: an alert, "this job moved", "this job finished". A single sender task
turns the queue into as few Telegram calls as it can, and waits out flood control instead of
losing messages:

- alerts that arrive close together go out as one message, and the same problem on many
  accounts (say X locking 40 of them at once) becomes one line listing the accounts;
- a job card is edited at most once per PROGRESS_INTERVAL, with its latest numbers;
- finished-job reports are sent in order and retried until Telegram takes them.

Before this, 70 accounts locked at the same moment meant 70 alerts plus card edits in one burst;
Telegram answered "retry after", the exception escaped into the worker, and most alerts and the
final reports were simply lost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

ALERT_DELAY = 3.0          # collect alerts this long before sending, so a burst becomes one message
PROGRESS_INTERVAL = 20.0   # at most one edit per job card this often
MAX_MESSAGE = 3800         # Telegram's limit is 4096; leave room
MAX_TRIES = 6
MAX_NAMES = 60             # accounts named per alert line before "… +N more"


class Target(Protocol):
    """What the outbox delivers to (the Telegram side, BotApp)."""

    async def send_alert(self, text: str) -> None: ...
    async def render_progress(self, job_id: int) -> None: ...
    async def report_finished(self, job_id: int) -> None: ...
    async def batch_of(self, job_id: int) -> int | None: ...


@dataclass
class _Alert:
    text: str
    group: str | None = None
    who: str | None = None


@dataclass
class _Pending:
    alerts: list[_Alert] = field(default_factory=list)
    progress: set[int] = field(default_factory=set)     # job ids whose card should be redrawn
    finished: list[int] = field(default_factory=list)   # job ids, in order


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds Telegram asked us to wait, if this is a flood-control error."""
    value = getattr(exc, "retry_after", None)
    return float(value) if isinstance(value, (int, float)) else None


def digest(alerts: list[_Alert]) -> list[str]:
    """Alerts as message texts: grouped ones merged into one line naming every account."""
    lines: list[str] = []
    groups: dict[str, list[str]] = {}
    for alert in alerts:
        if alert.group and alert.who:
            if alert.group not in groups:
                groups[alert.group] = []
                lines.append(alert.group)  # placeholder keeps the order alerts arrived in
            if alert.who not in groups[alert.group]:
                groups[alert.group].append(alert.who)
        else:
            lines.append(alert.text)
    rendered = []
    singles = {a.group: a.text for a in alerts if a.group}
    for line in lines:
        names = groups.get(line)
        if names is None:
            rendered.append(line)
        elif len(names) == 1:
            rendered.append(singles[line])
        else:
            shown = ", ".join(names[:MAX_NAMES]) + (f", … +{len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else "")
            rendered.append(f"{line} ({len(names)} accounts): {shown}")
    # Pack lines into as few messages as fit.
    messages, current = [], ""
    for line in rendered:
        while len(line) > MAX_MESSAGE:  # one huge line: split it
            if current:
                messages.append(current)
                current = ""
            messages.append(line[:MAX_MESSAGE])
            line = line[MAX_MESSAGE:]
        if current and len(current) + 2 + len(line) > MAX_MESSAGE:
            messages.append(current)
            current = ""
        current = f"{current}\n\n{line}" if current else line
    if current:
        messages.append(current)
    return messages


class Outbox:
    """The worker's Notifier. Never raises and never waits on Telegram."""

    def __init__(
        self,
        target: Target | None,
        *,
        alert_delay: float = ALERT_DELAY,
        progress_interval: float = PROGRESS_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.target = target
        self.alert_delay = alert_delay
        self.progress_interval = progress_interval
        self.clock = clock
        self.sleep = sleep
        self._pending = _Pending()
        self._wake = asyncio.Event()
        self._last_render: dict[int, float] = {}  # batch id -> when its card was last edited

    # ---- Notifier (called by the worker) ----

    async def alert(self, text: str, *, group: str | None = None, who: str | None = None) -> None:
        self._pending.alerts.append(_Alert(text, group, who))
        self._wake.set()

    async def job_progress(self, job_id: int) -> None:
        self._pending.progress.add(job_id)
        self._wake.set()

    async def job_finished(self, job_id: int) -> None:
        self._pending.finished.append(job_id)
        self._wake.set()

    # ---- sender ----

    def has_pending(self) -> bool:
        p = self._pending
        return bool(p.alerts or p.progress or p.finished)

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._pending.alerts:
                await self.sleep(self.alert_delay)  # let the rest of a burst arrive
            try:
                wait = await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox flush failed")
                wait = 5.0
            if wait is not None:
                # Cards still waiting for their interval: come back when the first is due.
                asyncio.get_running_loop().call_later(wait, self._wake.set)

    async def flush(self) -> float | None:
        """Send what is due. Returns seconds until a held-back card edit is due, if any."""
        alerts, self._pending.alerts = self._pending.alerts, []
        for text in digest(alerts):
            await self._deliver("alert", lambda text=text: self.target.send_alert(text))

        finished, self._pending.finished = self._pending.finished, []
        for job_id in finished:
            self._pending.progress.discard(job_id)
            await self._deliver("job report", lambda job_id=job_id: self.target.report_finished(job_id))

        held: list[float] = []
        seen_batches: set[int] = set()
        for job_id in sorted(self._pending.progress):
            batch = await self.target.batch_of(job_id)
            if batch is None or batch in seen_batches:
                self._pending.progress.discard(job_id)
                continue
            due = self._last_render.get(batch, float("-inf")) + self.progress_interval
            now = self.clock()
            if now < due:
                held.append(due - now)
                continue
            seen_batches.add(batch)
            self._pending.progress.discard(job_id)
            self._last_render[batch] = now
            await self._deliver("job card", lambda job_id=job_id: self.target.render_progress(job_id))
        # Other dirty jobs of a batch just drawn are covered by that edit.
        self._pending.progress = {
            j for j in self._pending.progress if (await self.target.batch_of(j)) not in seen_batches
        }
        return min(held) if held else None

    async def _deliver(self, what: str, send: Callable[[], Awaitable[None]]) -> bool:
        for attempt in range(1, MAX_TRIES + 1):
            try:
                await send()
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait = retry_after_seconds(exc)
                if wait is None or attempt == MAX_TRIES:
                    log.warning("couldn't send %s: %s", what, type(exc).__name__)
                    return False
                log.info("Telegram flood control: waiting %.0f s before sending %s", wait, what)
                await self.sleep(wait + 1)
        return False
