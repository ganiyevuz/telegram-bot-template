from __future__ import annotations
from typing import TYPE_CHECKING

from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from aiogram.utils.chat_action import ChatActionMiddleware
from aiogram.utils.i18n.core import I18n

from bot.analytics.types import AbstractAnalyticsLogger
from bot.core.config import Settings

if TYPE_CHECKING:
    from aiogram import Dispatcher
    from dishka import AsyncContainer


async def register_middlewares(dp: Dispatcher, container: AsyncContainer) -> None:
    """Register the update pipeline. Order is load-bearing — see spec section 7."""
    from .analytics import AnalyticsMiddleware  # noqa: PLC0415
    from .auth import AuthMiddleware  # noqa: PLC0415
    from .i18n import ACLMiddleware  # noqa: PLC0415
    from .logging import LoggingMiddleware  # noqa: PLC0415
    from .throttling import ThrottlingMiddleware  # noqa: PLC0415

    settings = await container.get(Settings)
    i18n = await container.get(I18n)
    gateway = await container.get(AbstractAnalyticsLogger)

    dp.update.outer_middleware(LoggingMiddleware())
    dp.message.outer_middleware(ThrottlingMiddleware(settings.bot.rate_limit))

    dp.message.middleware(AuthMiddleware())
    ACLMiddleware(i18n=i18n).setup(dp)
    dp.message.middleware(AnalyticsMiddleware(gateway))
    dp.callback_query.middleware(AnalyticsMiddleware(gateway))
    dp.message.middleware(ChatActionMiddleware())
    dp.callback_query.middleware(CallbackAnswerMiddleware())
