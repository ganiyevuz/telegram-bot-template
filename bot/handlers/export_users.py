# ruff: noqa: TC002  - dishka's AutoInjectMiddleware calls get_type_hints() on every
# handler's full signature at router startup (auto_inject=True in bot/core/lifespan.py), so
# every annotation on a handler must be importable at module level, never under TYPE_CHECKING
from __future__ import annotations
import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from loguru import logger
from redis.exceptions import RedisError

from bot.filters.admin import AdminFilter
from bot.tasks.export import export_users

router = Router(name="export_users")

# `.kiq()` enqueues over the same broker whose client-side socket timeout is disabled
# (see bot/tasks/__init__.py), so an unresponsive Redis would otherwise block this
# webhook request indefinitely. Bound it and fail the request instead of hanging it.
ENQUEUE_TIMEOUT = 5.0


@router.message(Command(commands="export_users"), AdminFilter())
async def export_users_handler(message: Message) -> None:
    """Queue a CSV export; the worker delivers the file when it is ready."""
    try:
        await asyncio.wait_for(export_users.kiq(chat_id=message.chat.id), timeout=ENQUEUE_TIMEOUT)
    except (TimeoutError, RedisError) as exc:
        logger.warning(f"export enqueue failed | chat_id: {message.chat.id} | error: {exc}")
        await message.answer(_("could not queue, try again later"))
        return
    await message.answer(_("export queued"))
