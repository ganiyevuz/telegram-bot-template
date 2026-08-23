from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from dishka.integrations.aiogram import setup_dishka
from loguru import logger
from sentry_sdk.integrations.loguru import LoggingLevels, LoguruIntegration

from bot.core.di import create_container
from bot.core.logging import setup_logging
from bot.handlers import get_handlers_router
from bot.handlers.errors import register_error_handler
from bot.middlewares import register_middlewares

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from dishka import AsyncContainer

    from bot.core.config import Settings


@dataclass(slots=True)
class AppContext:
    container: AsyncContainer
    bot: Bot
    dp: Dispatcher


def _setup_sentry(settings: Settings) -> None:
    if not settings.observability.sentry_dsn:
        return
    sentry_sdk.init(
        dsn=settings.observability.sentry_dsn,
        enable_tracing=True,
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0,
        integrations=[
            LoguruIntegration(level=LoggingLevels.INFO.value, event_level=LoggingLevels.ERROR.value),
        ],
    )


async def build_dispatcher(container: AsyncContainer) -> Dispatcher:
    storage = await container.get(RedisStorage)
    dp = Dispatcher(storage=storage)
    setup_dishka(container=container, router=dp, auto_inject=True)
    await register_middlewares(dp, container)
    dp.include_router(get_handlers_router())
    register_error_handler(dp)
    return dp


@asynccontextmanager
async def lifespan(settings: Settings) -> AsyncIterator[AppContext]:
    """Startup and shutdown shared by every entrypoint."""
    setup_logging(settings)
    _setup_sentry(settings)

    container = create_container(settings)
    try:
        bot = await container.get(Bot)
        dp = await build_dispatcher(container)

        info = await bot.get_me()
        logger.info(f"bot started | @{info.username} | id: {info.id}")
        yield AppContext(container=container, bot=bot, dp=dp)
    finally:
        logger.info("shutting down")
        await container.close()
        logger.info("shutdown complete")
