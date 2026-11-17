"""Import cookie exports from ./secrets at startup, so accounts can be set up without pasting into Telegram.

A file is imported only when its content changed since the last successful import. Unchanged files
are skipped on restart, so cookies the platform rotated since then aren't overwritten by stale ones.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Awaitable, Callable

from .config import PLATFORM_NAMES, Settings
from .cookies import CookieError, parse_cookie_export
from .crypto import CookieVault
from .db import Database
from .platforms import PlatformError, make_adapter
from .worker import AdapterFactory, ProxyMissing, Worker, proxy_url, vault_context

log = logging.getLogger(__name__)


async def import_cookie_files(
    settings: Settings,
    db: Database,
    vault: CookieVault,
    worker: Worker,
    report: Callable[[str], Awaitable[None]],
    adapter_factory: AdapterFactory = make_adapter,
) -> None:
    for platform, path in settings.cookie_files.items():
        name = PLATFORM_NAMES[platform]
        file_name = os.path.basename(path)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            await report(f"❌ {name}: can't read secrets/{file_name} ({exc.strerror}).")
            continue

        # The user agent is part of the import: changing it re-imports the same file.
        digest = hashlib.sha256(raw + settings.user_agent_for(platform).encode()).hexdigest()
        if digest == await db.last_import_hash(platform):
            continue

        try:
            cookies = parse_cookie_export(raw, platform)
            adapter = adapter_factory(
                platform, cookies, settings.user_agent_for(platform), proxy_url(settings, settings.import_location)
            )
        except (CookieError, ProxyMissing) as exc:
            await report(f"❌ {name}: secrets/{file_name}: {exc}")
            continue

        try:
            username = await adapter.verify()
            cookies = adapter.export_cookies() or cookies
        except PlatformError as exc:
            await report(f"❌ {name}: secrets/{file_name} didn't work: {exc.detail}. Re-export it, then restart.")
            continue
        except Exception as exc:
            log.warning("cookie import check failed: %s", type(exc).__name__)
            await report(f"❌ {name}: couldn't check secrets/{file_name}. See the bot log.")
            continue
        finally:
            await adapter.aclose()

        account_id = await db.upsert_account(
            platform,
            username,
            vault.encrypt(vault_context(platform, username), cookies),
            settings.user_agent_for(platform),
            settings.import_location,
        )
        await db.set_import_hash(platform, digest)
        await worker.forget(account_id)
        where = f" via 🌍 {settings.import_location}" if settings.import_location else ""
        await report(f"✅ Imported {name} @{username} from secrets/{file_name}{where}. Tap Follow accounts to start.")
