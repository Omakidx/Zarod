"""SQLite persistence: accounts, follow jobs/tasks, and the follow-request log used for rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY,
    platform       TEXT NOT NULL,
    username       TEXT NOT NULL,
    cookies_enc    TEXT NOT NULL,
    user_agent     TEXT NOT NULL,
    proxy_name     TEXT,
    status         TEXT NOT NULL DEFAULT 'active',   -- active | paused | expired | blocked
    paused_until   REAL NOT NULL DEFAULT 0,
    pause_reason   TEXT,
    next_action_at REAL NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    UNIQUE (platform, username)
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    batch_id    INTEGER,                             -- jobs started together share one (the first job's id)
    status      TEXT NOT NULL DEFAULT 'queued',      -- queued | done | cancelled
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    handle     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    detail     TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    done_at    REAL
);
CREATE INDEX IF NOT EXISTS tasks_pending ON tasks(account_id, status, id);
CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_account_at ON actions(account_id, at);
CREATE TABLE IF NOT EXISTS cookie_imports (
    platform TEXT PRIMARY KEY,
    sha256   TEXT NOT NULL      -- hash of the last successfully imported file
);
"""

# Task statuses that end a task.
FINAL_STATUSES = ("followed", "requested", "already", "not_found", "unavailable", "failed", "cancelled")


@dataclass
class Account:
    id: int
    platform: str
    username: str
    cookies_enc: str
    user_agent: str
    proxy_name: str | None
    status: str
    paused_until: float
    pause_reason: str | None
    next_action_at: float


@dataclass
class Task:
    id: int
    job_id: int
    account_id: int
    handle: str
    attempts: int


@dataclass
class Job:
    id: int
    account_id: int
    chat_id: int
    message_id: int | None
    status: str
    batch_id: int | None = None


def _job(row) -> Job:
    return Job(row["id"], row["account_id"], row["chat_id"], row["message_id"], row["status"], row["batch_id"] or row["id"])


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(SCHEMA)
        # Databases created before batches existed.
        columns = {r["name"] for r in await (await conn.execute("PRAGMA table_info(jobs)")).fetchall()}
        if "batch_id" not in columns:
            await conn.execute("ALTER TABLE jobs ADD COLUMN batch_id INTEGER")
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ---- accounts ----

    async def upsert_account(
        self, platform: str, username: str, cookies_enc: str, user_agent: str, proxy_name: str | None
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO accounts (platform, username, cookies_enc, user_agent, proxy_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (platform, username) DO UPDATE SET
                cookies_enc = excluded.cookies_enc,
                user_agent = excluded.user_agent,
                -- an account keeps its VPN location (and so its IP) once it has one
                proxy_name = COALESCE(accounts.proxy_name, excluded.proxy_name),
                status = 'active', paused_until = 0, pause_reason = NULL
            RETURNING id
            """,
            (platform, username, cookies_enc, user_agent, proxy_name, time.time()),
        )
        row = await cur.fetchone()
        await self.conn.commit()
        return row["id"]

    async def get_account(self, account_id: int) -> Account | None:
        cur = await self.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = await cur.fetchone()
        return _account(row) if row else None

    async def list_accounts(self, platform: str | None = None) -> list[Account]:
        if platform:
            cur = await self.conn.execute("SELECT * FROM accounts WHERE platform = ? ORDER BY id", (platform,))
        else:
            cur = await self.conn.execute("SELECT * FROM accounts ORDER BY platform, id")
        return [_account(r) for r in await cur.fetchall()]

    async def set_account_location(self, account_id: int, proxy_name: str) -> None:
        await self.conn.execute("UPDATE accounts SET proxy_name = ? WHERE id = ?", (proxy_name, account_id))
        await self.conn.commit()

    async def delete_account(self, account_id: int) -> None:
        # Forget the cookie-file import too, so the next start imports that file again.
        await self.conn.execute(
            "DELETE FROM cookie_imports WHERE platform = (SELECT platform FROM accounts WHERE id = ?)", (account_id,)
        )
        await self.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        await self.conn.commit()

    async def update_cookies(self, account_id: int, cookies_enc: str) -> None:
        await self.conn.execute("UPDATE accounts SET cookies_enc = ? WHERE id = ?", (cookies_enc, account_id))
        await self.conn.commit()

    async def set_account_state(
        self,
        account_id: int,
        *,
        status: str | None = None,
        paused_until: float | None = None,
        pause_reason: str | None = None,
        next_action_at: float | None = None,
        clear_reason: bool = False,
    ) -> None:
        sets, args = [], []
        for col, val in (("status", status), ("paused_until", paused_until), ("next_action_at", next_action_at)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(val)
        if pause_reason is not None or clear_reason:
            sets.append("pause_reason = ?")
            args.append(pause_reason)
        if not sets:
            return
        await self.conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", (*args, account_id))
        await self.conn.commit()

    async def last_import_hash(self, platform: str) -> str | None:
        cur = await self.conn.execute("SELECT sha256 FROM cookie_imports WHERE platform = ?", (platform,))
        row = await cur.fetchone()
        return row["sha256"] if row else None

    async def set_import_hash(self, platform: str, sha256: str) -> None:
        await self.conn.execute(
            "INSERT INTO cookie_imports (platform, sha256) VALUES (?, ?) "
            "ON CONFLICT (platform) DO UPDATE SET sha256 = excluded.sha256",
            (platform, sha256),
        )
        await self.conn.commit()

    # ---- actions (rate-limit log) ----

    async def record_action(self, account_id: int, at: float) -> None:
        await self.conn.execute("INSERT INTO actions (account_id, at) VALUES (?, ?)", (account_id, at))
        await self.conn.execute("DELETE FROM actions WHERE at < ?", (at - 2 * 86_400,))
        await self.conn.commit()

    async def action_times(self, account_id: int, since: float) -> list[float]:
        cur = await self.conn.execute(
            "SELECT at FROM actions WHERE account_id = ? AND at > ? ORDER BY at", (account_id, since)
        )
        return [r["at"] for r in await cur.fetchall()]

    # ---- jobs & tasks ----

    async def create_job(self, account_id: int, chat_id: int, handles: list[str], batch_id: int | None = None) -> int:
        """One account's share of a follow request. Jobs started together pass the first job's id as batch_id."""
        cur = await self.conn.execute(
            "INSERT INTO jobs (account_id, chat_id, created_at) VALUES (?, ?, ?) RETURNING id",
            (account_id, chat_id, time.time()),
        )
        job_id = (await cur.fetchone())["id"]
        await self.conn.execute("UPDATE jobs SET batch_id = ? WHERE id = ?", (batch_id or job_id, job_id))
        await self.conn.executemany(
            "INSERT INTO tasks (job_id, account_id, handle) VALUES (?, ?, ?)",
            [(job_id, account_id, h) for h in handles],
        )
        await self.conn.commit()
        return job_id

    async def set_job_message(self, job_id: int, message_id: int) -> None:
        await self.conn.execute("UPDATE jobs SET message_id = ? WHERE id = ?", (message_id, job_id))
        await self.conn.commit()

    async def get_job(self, job_id: int) -> Job | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return _job(row) if row else None

    async def batch_jobs(self, batch_id: int) -> list[Job]:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE batch_id = ? OR id = ? ORDER BY id", (batch_id, batch_id))
        return [_job(r) for r in await cur.fetchall()]

    async def open_batches(self) -> list[int]:
        cur = await self.conn.execute(
            "SELECT DISTINCT COALESCE(batch_id, id) AS b FROM jobs WHERE status = 'queued' ORDER BY b")
        return [r["b"] for r in await cur.fetchall()]

    async def accounts_with_pending(self) -> list[int]:
        cur = await self.conn.execute("SELECT DISTINCT account_id FROM tasks WHERE status = 'pending'")
        return [r["account_id"] for r in await cur.fetchall()]

    async def next_task(self, account_id: int) -> Task | None:
        cur = await self.conn.execute(
            "SELECT id, job_id, account_id, handle, attempts FROM tasks "
            "WHERE account_id = ? AND status = 'pending' ORDER BY id LIMIT 1",
            (account_id,),
        )
        row = await cur.fetchone()
        return Task(row["id"], row["job_id"], row["account_id"], row["handle"], row["attempts"]) if row else None

    async def finish_task(self, task_id: int, status: str, detail: str = "") -> None:
        await self.conn.execute(
            "UPDATE tasks SET status = ?, detail = ?, done_at = ? WHERE id = ?",
            (status, detail, time.time(), task_id),
        )
        await self.conn.commit()

    async def bump_attempts(self, task_id: int, detail: str) -> int:
        cur = await self.conn.execute(
            "UPDATE tasks SET attempts = attempts + 1, detail = ? WHERE id = ? RETURNING attempts",
            (detail, task_id),
        )
        attempts = (await cur.fetchone())["attempts"]
        await self.conn.commit()
        return attempts

    async def job_counts(self, job_id: int) -> dict[str, int]:
        cur = await self.conn.execute("SELECT status, COUNT(*) AS n FROM tasks WHERE job_id = ? GROUP BY status", (job_id,))
        return {r["status"]: r["n"] for r in await cur.fetchall()}

    async def job_task_details(self, job_id: int, statuses: tuple[str, ...]) -> list[tuple[str, str, str]]:
        marks = ",".join("?" * len(statuses))
        cur = await self.conn.execute(
            f"SELECT handle, status, COALESCE(detail, '') AS detail FROM tasks "
            f"WHERE job_id = ? AND status IN ({marks}) ORDER BY id",
            (job_id, *statuses),
        )
        return [(r["handle"], r["status"], r["detail"]) for r in await cur.fetchall()]

    async def finish_job_if_complete(self, job_id: int) -> bool:
        cur = await self.conn.execute(
            "UPDATE jobs SET status = 'done' WHERE id = ? AND status = 'queued' "
            "AND NOT EXISTS (SELECT 1 FROM tasks WHERE job_id = ? AND status = 'pending') RETURNING id",
            (job_id, job_id),
        )
        done = await cur.fetchone() is not None
        await self.conn.commit()
        return done

    async def cancel_pending(self, account_id: int | None = None) -> int:
        where, args = "status = 'pending'", []
        if account_id is not None:
            where += " AND account_id = ?"
            args.append(account_id)
        cur = await self.conn.execute(
            f"UPDATE tasks SET status = 'cancelled', done_at = ? WHERE {where} RETURNING job_id", (time.time(), *args)
        )
        job_ids = {r["job_id"] for r in await cur.fetchall()}
        for job_id in job_ids:
            await self.conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ? AND status = 'queued'", (job_id,))
        await self.conn.commit()
        return len(job_ids)

    async def followed_before(self, account_id: int, handle: str) -> bool:
        """Whether this account already followed (or requested) this handle in an earlier task."""
        cur = await self.conn.execute(
            "SELECT 1 FROM tasks WHERE account_id = ? AND lower(handle) = lower(?) "
            "AND status IN ('followed', 'requested', 'already') LIMIT 1",
            (account_id, handle),
        )
        return await cur.fetchone() is not None

    async def pending_handles(self, account_id: int) -> set[str]:
        cur = await self.conn.execute(
            "SELECT handle FROM tasks WHERE account_id = ? AND status = 'pending'", (account_id,)
        )
        return {r["handle"].lower() for r in await cur.fetchall()}

    async def pending_count(self, account_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE account_id = ? AND status = 'pending'", (account_id,)
        )
        return (await cur.fetchone())["n"]


def _account(row: aiosqlite.Row) -> Account:
    return Account(
        id=row["id"],
        platform=row["platform"],
        username=row["username"],
        cookies_enc=row["cookies_enc"],
        user_agent=row["user_agent"],
        proxy_name=row["proxy_name"],
        status=row["status"],
        paused_until=row["paused_until"],
        pause_reason=row["pause_reason"],
        next_action_at=row["next_action_at"],
    )
