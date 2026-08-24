# ruff: noqa: TC002  - dishka resolves this handler's signature at runtime via
# get_type_hints(), so every injected annotation must be importable at module level
from __future__ import annotations
import traceback
from typing import TYPE_CHECKING

import sentry_sdk
from aiogram import Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ErrorEvent, Message
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka
from loguru import logger

from bot.core.logging import correlation_id
from bot.middlewares.metrics import HANDLER_ERRORS
from bot.notifier import AlertLevel, NotifierService

if TYPE_CHECKING:
    from aiogram import Dispatcher

router = Router(name="errors")

# Telegram caps a message at 4096 characters and `AlertLevel.render` HTML-escapes the
# body into a <pre> block, which only makes it longer. Keep the *tail* of the traceback:
# the frames nearest the raise are the ones worth reading, and the outer frames are
# aiogram's dispatch machinery, identical in every alert.
TRACEBACK_LIMIT = 2000


def _crash_site(exception: BaseException) -> tuple[str, int]:
    """Module and line of the frame the exception was actually raised in.

    The fingerprint is built from this rather than from the exception message: one alert
    then covers one crash site, however many updates hit it, instead of one per update —
    and a message that interpolates a user id or a chat title cannot fragment the
    cooldown into a fingerprint per user.
    """
    tb = exception.__traceback__
    if tb is None:
        # Only reachable for an exception object raised nowhere, e.g. one constructed and
        # passed to a handler by hand. Alert anyway, under a fingerprint of its own.
        return "unknown", 0
    while tb.tb_next is not None:
        tb = tb.tb_next
    return str(tb.tb_frame.f_globals.get("__name__", "unknown")), tb.tb_lineno


@router.error()
async def on_error(event: ErrorEvent, notifier: FromDishka[NotifierService]) -> bool:
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

    module, lineno = _crash_site(exception)
    try:
        await notifier.send(
            AlertLevel.ERROR,
            f"{type(exception).__name__} at {module}:{lineno}",
            f"cid: {cid}\n{''.join(traceback.format_exception(exception))[-TRACEBACK_LIMIT:]}",
            f"{type(exception).__name__}:{module}:{lineno}",
        )
    except Exception as exc:  # noqa: BLE001 - alerting must not become a source of errors
        # This handler returning True is what makes the webhook answer 200. If the alert
        # were allowed to propagate, every *handled* error would become an unhandled one
        # and Telegram would redeliver the same update forever — the notifier failing
        # would take the bot down with it, which is the opposite of its job.
        logger.warning(f"failed to alert on error | cid: {cid} | {type(exc).__name__}: {exc}")

    target = event.update.message or event.update.callback_query
    try:
        if isinstance(target, Message):
            await target.answer(_("something went wrong"))
        elif isinstance(target, CallbackQuery):
            await target.answer(_("something went wrong"), show_alert=True)
    except TelegramAPIError as exc:
        # The apology itself can fail — the user blocked the bot, the update carries no
        # chat to reply into, or Telegram is rate-limiting us. The original fault is
        # already logged and reported above; losing the apology is not worth raising
        # again, which in webhook mode would turn into another HTTP 500 and a Telegram
        # retry storm.
        logger.warning(f"failed to notify the user | cid: {cid} | {type(exc).__name__}: {exc}")
    return True


def register_error_handler(dp: Dispatcher) -> None:
    dp.include_router(router)
