from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from followbot.outbox import Outbox, digest, _Alert
from followbot.platforms.base import ActionBlocked, FollowResult, Outcome

from test_worker import FakeAdapter, drain, env  # noqa: F401


def flood(seconds=7):
    return TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="Too Many Requests", retry_after=seconds)


class Target:
    def __init__(self, fail_first=0):
        self.alerts, self.cards, self.reports = [], [], []
        self.fail_first = fail_first

    def _maybe_flood(self):
        if self.fail_first:
            self.fail_first -= 1
            raise flood()

    async def send_alert(self, text):
        self._maybe_flood()
        self.alerts.append(text)

    async def render_progress(self, job_id):
        self._maybe_flood()
        self.cards.append(job_id)

    async def report_finished(self, job_id):
        self._maybe_flood()
        self.reports.append(job_id)

    async def batch_of(self, job_id):
        return job_id // 10  # jobs 10..19 are batch 1, 20..29 batch 2


class Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def outbox(target, clock):
    return Outbox(target, alert_delay=0, progress_interval=20, clock=clock, sleep=clock.sleep)


async def test_a_burst_of_the_same_problem_is_one_message_naming_every_account():
    target, clock = Target(), Clock()
    box = outbox(target, clock)
    for n in range(70):
        await box.alert(f"⛔ @acc{n} (x): X locked it. Then tap Resume all.",
                        group="⛔ X locked it. Then tap Resume all.", who=f"@acc{n} (x)")
    await box.alert("🔑 @other (instagram): logged out.", group="🔑 logged out.", who="@other (instagram)")
    await box.flush()
    assert len(target.alerts) == 1
    text = target.alerts[0]
    assert "⛔ X locked it. Then tap Resume all. (70 accounts): @acc0 (x), @acc1 (x)" in text
    assert "… +10 more" in text  # names are capped, the count is not
    assert "🔑 @other (instagram): logged out." in text  # a single one keeps its own wording


def test_long_digests_are_split_under_telegrams_limit():
    alerts = [_Alert(f"line {n} " + "x" * 300) for n in range(40)]
    messages = digest(alerts)
    assert len(messages) > 1 and all(len(m) <= 3800 for m in messages)
    assert "".join(messages).count("line ") == 40


async def test_flood_control_waits_and_delivers_instead_of_losing_the_message():
    target, clock = Target(fail_first=2), Clock()
    box = outbox(target, clock)
    await box.job_finished(15)
    await box.flush()
    assert target.reports == [15]
    assert clock.slept == [8, 8]  # retry_after + 1, twice


async def test_job_card_edits_are_throttled_per_batch():
    target, clock = Target(), Clock()
    box = outbox(target, clock)
    for job in (11, 12, 13, 21):  # three accounts of batch 1, one of batch 2
        await box.job_progress(job)
    assert await box.flush() is None
    assert sorted(target.cards) == [11, 21]  # one edit per card

    await box.job_progress(12)
    wait = await box.flush()
    assert target.cards.count(11) + target.cards.count(12) == 1 and wait == 20  # held back
    clock.t += 20
    assert await box.flush() is None
    assert target.cards[-1] == 12  # the held edit goes out with the latest numbers


async def test_a_finished_report_replaces_a_pending_card_edit():
    target, clock = Target(), Clock()
    box = outbox(target, clock)
    await box.job_progress(11)
    await box.job_finished(11)
    await box.flush()
    assert target.reports == [11] and target.cards == []


class Exploding:
    """A notifier that fails the way Telegram flood control did in production."""

    def __init__(self):
        self.calls = 0

    async def _boom(self, *args, **kwargs):
        self.calls += 1
        raise flood()

    job_progress = job_finished = alert = _boom


async def test_a_failing_notifier_never_undoes_or_stops_follows(env):
    db, worker, clock, notes, holder, acc = env
    worker.notifier = Exploding()
    holder["adapter"] = FakeAdapter({})
    job = await db.create_job(acc, 99, ["a", "b"])
    await drain(worker, clock)
    assert await db.job_counts(job) == {"followed": 2}
    assert worker.notifier.calls >= 2


async def test_a_locked_account_is_held_even_when_its_alert_cannot_be_sent(env):
    db, worker, clock, notes, holder, acc = env
    worker.notifier = Exploding()

    class Locked(FakeAdapter):
        async def follow(self, handle):
            raise ActionBlocked("X has locked this account.")

    holder["adapter"] = Locked({})
    await db.create_job(acc, 99, ["a"])
    await worker.tick()
    account = await db.get_account(acc)
    assert account.status == "blocked" and "locked" in account.pause_reason
    assert await db.pending_count(acc) == 1  # kept for when you unlock it and tap Resume all
