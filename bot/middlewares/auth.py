from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import Message
from loguru import logger

from bot.services.users import UserService
from bot.utils.command import find_command_argument

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject
    from dishka import AsyncContainer


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        container: AsyncContainer = data["dishka_container"]
        users = await container.get(UserService)
        referrer = find_command_argument(event.text)

        if await users.ensure_registered(event.from_user, referrer):
            logger.info(f"new user registration | user_id: {event.from_user.id} | referrer: {referrer}")

        data["user_service"] = users
        return await handler(event, data)
