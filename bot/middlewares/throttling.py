from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from loguru import logger

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

    from bot.cache.ratelimit import TokenBucket


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user rate limiting shared across replicas.

    Keyed by user rather than chat, so members of a group do not consume each
    other's allowance.
    """

    def __init__(self, bucket: TokenBucket) -> None:
        self._bucket = bucket
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        if not await self._bucket.try_acquire(CacheKeys.throttle("user", user.id)):
            logger.debug(f"throttled | user_id: {user.id}")
            return None

        return await handler(event, data)
