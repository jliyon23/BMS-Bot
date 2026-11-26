from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bmsbot.bms.client import BmsClient
from bmsbot.bms.http import AsyncRateLimiter
from bmsbot.bot.handlers import onboarding, watches
from bmsbot.bot.handlers import settings as settings_handler
from bmsbot.bot.middlewares import DbSessionMiddleware, RateLimitMiddleware, UserMiddleware
from bmsbot.bot.storage import SQLiteFSMStorage
from bmsbot.config import Settings, get_settings
from bmsbot.db.session import session_factory
from bmsbot.logging import configure_logging
from bmsbot.proxies.pool import ProxyPool
from bmsbot.services.regions import sync_regions
from bmsbot.services.runtime import RuntimeFlags

log = logging.getLogger(__name__)


def create_dispatcher(
    settings: Settings,
    client: BmsClient,
    session_factory,
) -> Dispatcher:
    dispatcher = Dispatcher(storage=SQLiteFSMStorage(session_factory))
    dispatcher["client"] = client
    dispatcher["settings"] = settings

    dispatcher.update.outer_middleware(DbSessionMiddleware(session_factory))
    dispatcher.update.outer_middleware(UserMiddleware())
    dispatcher.update.outer_middleware(RateLimitMiddleware(limit=30, window_sec=60))

    dispatcher.include_router(onboarding.router)
    dispatcher.include_router(watches.router)
    dispatcher.include_router(settings_handler.router)

    @dispatcher.startup()
    async def _startup() -> None:
        async with session_factory() as session:
            try:
                await sync_regions(session, client)
                await session.commit()
            except Exception:
                log.warning("could not sync regions at startup (no proxy?)")

    return dispatcher


async def run_bot() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("BMS_TELEGRAM_BOT_TOKEN is not configured")

    pool = ProxyPool(session_factory, settings)
    flags = RuntimeFlags(session_factory)
    client = BmsClient(
        settings,
        pool,
        limiter=AsyncRateLimiter(settings.max_requests_per_sec),
        proxy_enabled=flags.use_proxy,
    )
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = create_dispatcher(settings, client, session_factory)
    await dispatcher.start_polling(bot)


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
