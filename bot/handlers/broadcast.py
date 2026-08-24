# ruff: noqa: TC002  - dishka's AutoInjectMiddleware calls get_type_hints() on every
# handler's full signature at router startup (auto_inject=True in bot/core/lifespan.py), so
# every annotation on a handler must be importable at module level, never under TYPE_CHECKING
from __future__ import annotations
import asyncio
from uuid import uuid4

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from loguru import logger
from redis.exceptions import RedisError

from bot.filters.admin import AdminFilter
from bot.tasks.broadcast import start_broadcast

router = Router(name="broadcast")

# See bot/handlers/export_users.py for why enqueues must be bounded.
ENQUEUE_TIMEOUT = 5.0


@router.message(Command(commands="broadcast"), AdminFilter())
async def broadcast_handler(message: Message, command: CommandObject) -> None:
    """Queue a broadcast: /broadcast <text>."""
    if not command.args:
        await message.answer(_("usage: /broadcast <text>"))
        return

    broadcast_id = uuid4().hex
    try:
        await asyncio.wait_for(
            start_broadcast.kiq(broadcast_id=broadcast_id, text=command.args, initiator_id=message.chat.id),
            timeout=ENQUEUE_TIMEOUT,
        )
    except (TimeoutError, RedisError) as exc:
        logger.warning(f"broadcast enqueue failed | id: {broadcast_id} | error: {exc}")
        await message.answer(_("could not queue, try again later"))
        return
    await message.answer(_("broadcast queued: <code>{id}</code>").format(id=broadcast_id))
