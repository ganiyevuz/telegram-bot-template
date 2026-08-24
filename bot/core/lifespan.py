from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n.core import I18n
from dishka.integrations.aiogram import setup_dishka
from loguru import logger
from sentry_sdk.integrations.loguru import LoggingLevels, LoguruIntegration

from bot.core.di import create_container
from bot.core.logging import setup_logging
from bot.handlers import get_handlers_router
from bot.handlers.errors import register_error_handler
from bot.middlewares import register_middlewares
from bot.notifier import AlertLevel, NotifierService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from dishka import AsyncContainer

    from bot.core.config import Settings


@dataclass(slots=True)
class AppContext:
    container: AsyncContainer
    bot: Bot
    dp: Dispatcher
    i18n: I18n


async def _alert(notifier: NotifierService, level: AlertLevel, title: str, body: str, fingerprint: str) -> None:
    """Report a lifecycle event, and never let reporting it become the reason it fails.

    The startup call sits between `get_me()` and the `yield` that hands the app to its
    entrypoint, and the shutdown call is inside the `finally` that closes the container:
    an exception from either would abort a healthy boot or mask the real shutdown error
    and skip `container.close()`.
    """
    try:
        await notifier.send(level, title, body, fingerprint)
    except Exception as exc:  # noqa: BLE001 - alerting must not become a source of errors
        logger.warning(f"lifecycle alert failed | {title} | {type(exc).__name__}: {exc}")


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
    # Resolved before the `try`, so the `finally` below can tell "never got that far"
    # from "built fine": a container that fails on `get(Bot)` has no notifier to alert
    # its own shutdown with.
    notifier: NotifierService | None = None
    try:
        bot = await container.get(Bot)
        i18n = await container.get(I18n)
        notifier = await container.get(NotifierService)
        dp = await build_dispatcher(container)

        info = await bot.get_me()
        logger.info(f"bot started | @{info.username} | id: {info.id}")
        # Fires on every replica, so a three-replica rolling deploy sends six of these.
        # That is what the fingerprint cooldown is for — a second suppression mechanism
        # here would only make the *first* deploy of the day silent too.
        await _alert(notifier, AlertLevel.INFO, "bot started", f"@{info.username} | id: {info.id}", "lifecycle:startup")
        yield AppContext(container=container, bot=bot, dp=dp, i18n=i18n)
    finally:
        logger.info("shutting down")
        if notifier is not None:
            # Before `container.close()`: the notifier writes its cooldown marker and
            # enqueues the delivery task through the Redis client that call tears down.
            await _alert(notifier, AlertLevel.INFO, "bot shutting down", "process is terminating", "lifecycle:shutdown")
        await container.close()
        logger.info("shutdown complete")
