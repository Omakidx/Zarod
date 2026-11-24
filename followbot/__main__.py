"""Entry point.

  python -m followbot              run the bot
  python -m followbot keygen       print a new ENCRYPTION_KEY
  python -m followbot probe x - --location us [--follow HANDLE] < cookies.json
                                   check cookies (and optionally follow one account) without Telegram
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import logging
import os
import sys

from .config import DEFAULT_USER_AGENT, ConfigError, load_settings
from .crypto import generate_key

log = logging.getLogger("followbot")


async def run_bot() -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage

    from .bot import BotApp, setup_commands
    from .crypto import CookieVault
    from .db import Database
    from .importer import import_cookie_files
    from .ui import IconFallback
    from .config import PLATFORM_NAMES
    from .outbox import Outbox
    from .worker import Worker, spread_locations

    settings = load_settings()
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    db = await Database.open(settings.db_path)
    vault = CookieVault(settings.encryption_key)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(IconFallback())

    outbox = Outbox(None)  # its target, the bot app, needs the worker first
    worker = Worker(db, vault, settings, outbox)
    app = BotApp(bot, db, vault, settings, worker)
    outbox.target = app
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(app.router)

    await setup_commands(bot)

    async def report(text: str) -> None:
        log.info("startup: %s", text)
        try:
            await app.alert(text)
        except Exception as exc:  # e.g. you haven't pressed /start in the bot yet
            log.warning("couldn't message the owner: %s", type(exc).__name__)

    await worker.remove_blocked_leftovers()  # before spreading, so their locations are free again
    moves = await spread_locations(db, settings)
    if moves:
        # Worker adapters are built after this, so moved accounts start on their new IP.
        per_ip = max(Counter(a.proxy_name for a in await db.list_accounts()).values())
        names = ", ".join(f"@{a.username}→{loc}" for a, loc in moves[:20]) + (f", … +{len(moves) - 20}" if len(moves) > 20 else "")
        over = (f" That's over the limit of {settings.max_per_ip}: add more VPN locations."
                if per_ip > settings.max_per_ip else "")
        await report(f"🌍 Spread accounts over {len(settings.proxies)} VPN locations (moves stay in-country): "
                     f"moved {len(moves)} ({names}). Now at most {per_ip} accounts share an IP.{over}")
    await import_cookie_files(settings, db, vault, worker, report)
    outbox_task = asyncio.create_task(outbox.run(), name="outbox")
    worker_task = asyncio.create_task(worker.run(), name="follow-worker")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker_task.cancel()
        outbox_task.cancel()
        await worker.close()
        await db.close()
        await bot.session.close()


def probe_proxy(env: dict[str, str], location: str | None, direct: bool) -> str | None:
    """Same kill-switch as the bot: refuse to touch the platforms without the VPN unless --direct."""
    from .config import _parse_proxies

    if direct:
        return None
    proxies = _parse_proxies(env.get("PROXIES", ""))
    if not location:
        raise ConfigError(f"Pass --location ({', '.join(proxies) or 'none configured'}) or --direct")
    if location not in proxies:
        raise ConfigError(f"Location {location!r} is not in PROXIES")
    return proxies[location]


async def probe(platform: str, cookie_file: str, proxy: str | None, handle: str | None) -> int:
    from .cookies import CookieError, parse_cookie_export
    from .platforms import PlatformError, make_adapter

    if cookie_file == "-":
        raw = sys.stdin.buffer.read()
    else:
        with open(cookie_file, "rb") as fh:
            raw = fh.read()
    try:
        cookies = parse_cookie_export(raw, platform)
    except CookieError as exc:
        print(f"Cookie error: {exc}", file=sys.stderr)
        return 1
    adapter = make_adapter(platform, cookies, os.environ.get("BROWSER_USER_AGENT") or DEFAULT_USER_AGENT, proxy)
    try:
        print(f"logged in as @{await adapter.verify()}")
        if handle:
            result = await adapter.follow(handle)
            print(f"@{handle}: {result.outcome.value} (request sent: {result.action_sent}) {result.detail}")
    except PlatformError as exc:
        print(f"{type(exc).__name__}: {exc.detail}", file=sys.stderr)
        return 1
    finally:
        await adapter.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="followbot")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("keygen")
    p = sub.add_parser("probe")
    p.add_argument("platform", choices=["x", "instagram"])
    p.add_argument("cookie_file", help="path to the cookie JSON, or - to read it from stdin")
    p.add_argument("--location", help="VPN location name from PROXIES")
    p.add_argument("--direct", action="store_true", help="skip the VPN (not recommended)")
    p.add_argument("--follow", dest="handle")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO; keep it quieter.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "keygen":
        print(generate_key())
        return 0
    if args.cmd == "probe":
        try:
            proxy = probe_proxy(dict(os.environ), args.location, args.direct)
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 2
        return asyncio.run(probe(args.platform, args.cookie_file, proxy, args.handle))
    try:
        asyncio.run(run_bot())
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
