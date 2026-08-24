from __future__ import annotations
from typing import TYPE_CHECKING

from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from aiogram.utils.chat_action import ChatActionMiddleware
from aiogram.utils.i18n.core import I18n
from loguru import logger
from redis.asyncio import Redis

from bot.analytics.types import AbstractAnalyticsLogger
from bot.core.config import Settings

if TYPE_CHECKING:
    from aiogram import Dispatcher
    from dishka import AsyncContainer


async def register_middlewares(dp: Dispatcher, container: AsyncContainer) -> None:
    """Register the update pipeline.

    Registration order here does NOT control runtime order: aiogram runs every
    OUTER middleware registered anywhere before any INNER one, regardless of
    where either was registered. aiogram also wires its own outer middlewares
    (an error boundary, `UserContextMiddleware`, `FSMContextMiddleware`) onto
    every `Dispatcher` before any middleware registered here runs. And its own
    `I18nMiddleware.setup()` — used by `ACLMiddleware` below — registers ACL as
    OUTER, so i18n resolves the locale before `AuthMiddleware` (INNER) sets the
    user. The measured chain on `dp.update` is: aiogram's own
    (error boundary, `UserContextMiddleware`, `FSMContextMiddleware`), then
    dishka's `ContainerMiddleware`, then ours: `DedupMiddleware`,
    `LoggingMiddleware`, `MetricsMiddleware`, `ACLMiddleware` (OUTER, via
    `.setup()`), then the INNER middlewares (`AuthMiddleware`, ...).
    """
    from .analytics import AnalyticsMiddleware  # noqa: PLC0415
    from .auth import AuthMiddleware  # noqa: PLC0415
    from .dedup import DedupMiddleware  # noqa: PLC0415
    from .i18n import ACLMiddleware  # noqa: PLC0415
    from .logging import LoggingMiddleware  # noqa: PLC0415
    from .metrics import MetricsMiddleware  # noqa: PLC0415
    from .throttling import ThrottlingMiddleware  # noqa: PLC0415

    i18n = await container.get(I18n)
    gateway = await container.get(AbstractAnalyticsLogger)
    redis = await container.get(Redis)
    settings = await container.get(Settings)

    dp.update.outer_middleware(DedupMiddleware(redis))
    dp.update.outer_middleware(LoggingMiddleware())
    dp.update.outer_middleware(MetricsMiddleware())

    # RATE_LIMIT <= 0 means "no inbound throttling" — skip registering the middleware
    # entirely rather than translating it into some internal rate, so it can't silently
    # collapse to a default limit an operator never asked for.
    if settings.bot.rate_limit > 0:
        # Imported lazily, not at module level: bot.core.di imports bot.telegram.factory,
        # which imports bot.telegram.ratelimit, which imports bot.middlewares.metrics —
        # and importing that submodule forces Python to run this package's __init__ first.
        # A module-level `from bot.core.di import ThrottleBucket` here would therefore hit
        # bot.core.di mid-initialization and fail with a circular-import error.
        from bot.core.di import ThrottleBucket  # noqa: PLC0415

        bucket = await container.get(ThrottleBucket)
        dp.message.outer_middleware(ThrottlingMiddleware(bucket))
    else:
        logger.info(f"inbound throttling disabled | RATE_LIMIT={settings.bot.rate_limit}")

    dp.message.middleware(AuthMiddleware())
    ACLMiddleware(i18n=i18n).setup(dp)
    dp.message.middleware(AnalyticsMiddleware(gateway))
    dp.callback_query.middleware(AnalyticsMiddleware(gateway))
    dp.message.middleware(ChatActionMiddleware())
    dp.callback_query.middleware(CallbackAnswerMiddleware())
