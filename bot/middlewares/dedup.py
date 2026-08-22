from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import Update
from loguru import logger

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject
    from redis.asyncio import Redis

DEDUP_TTL = 600


class DedupMiddleware(BaseMiddleware):
    """Drops updates already claimed by another replica.

    Must be the outermost middleware: anything registered before it does its
    work twice on a redelivered update.
    """

    def __init__(self, redis: Redis, ttl: int = DEDUP_TTL) -> None:
        self._redis = redis
        self._ttl = ttl
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        claimed = await self._redis.set(CacheKeys.dedup(event.update_id), 1, nx=True, ex=self._ttl)
        if not claimed:
            logger.warning(f"duplicate update dropped | update_id: {event.update_id}")
            return None

        return await handler(event, data)
