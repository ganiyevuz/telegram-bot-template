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
from bot.tasks.backup import run_backup

router = Router(name="admin_backup")

# `.kiq()` enqueues over the same broker whose client-side socket timeout is disabled
# (see bot/tasks/__init__.py), so an unresponsive Redis would otherwise block this
# webhook request indefinitely. Bound it and fail the request instead of hanging it.
ENQUEUE_TIMEOUT = 5.0


@router.message(Command(commands="backup"), AdminFilter())
async def backup_handler(message: Message) -> None:
    """/backup - queue an encrypted backup; the worker reports back into this chat.

    Enqueued, never run inline. Encrypting a database and uploading it in 45 MiB parts takes
    minutes; doing that here would blow past Telegram's handler window and hold the update
    pipeline shut for every other user in the meantime.
    """
    try:
        await asyncio.wait_for(run_backup.kiq(report_chat_id=message.chat.id), timeout=ENQUEUE_TIMEOUT)
    except (TimeoutError, RedisError) as exc:
        logger.warning(f"backup enqueue failed | chat_id: {message.chat.id} | error: {exc}")
        await message.answer(_("could not queue, try again later"))
        return
    await message.answer(_("backup started, you'll get a report in this chat"))
