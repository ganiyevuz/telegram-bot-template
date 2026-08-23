from __future__ import annotations
from typing import TYPE_CHECKING

import sentry_sdk
from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ErrorEvent, Message
from aiogram.utils.i18n import gettext as _
from loguru import logger

from bot.core.logging import correlation_id
from bot.middlewares.metrics import HANDLER_ERRORS

if TYPE_CHECKING:
    from aiogram import Dispatcher

router = Router(name="errors")


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    exception = event.exception
    cid = correlation_id.get() or "-"

    if isinstance(exception, TelegramRetryAfter | TelegramForbiddenError):
        # Expected and actionable elsewhere: the rate limiter handles the first,
        # and the broadcast task marks blocked users on the second.
        logger.warning(f"telegram rejected a call | cid: {cid} | {type(exception).__name__}: {exception}")
        return True

    HANDLER_ERRORS.labels(exception=type(exception).__name__).inc()
    logger.opt(exception=exception).error(f"unhandled error | cid: {cid}")
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("correlation_id", cid)
        scope.set_context("update", event.update.model_dump(exclude_none=True, mode="json"))
        sentry_sdk.capture_exception(exception)

    target = event.update.message or event.update.callback_query
    if isinstance(target, Message):
        await target.answer(_("something went wrong"))
    elif isinstance(target, CallbackQuery):
        await target.answer(_("something went wrong"), show_alert=True)
    return True


def register_error_handler(dp: Dispatcher) -> None:
    dp.include_router(router)
