from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import Update
from loguru import logger
from redis.exceptions import RedisError

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

        try:
            claimed = await self._redis.set(CacheKeys.dedup(event.update_id), 1, nx=True, ex=self._ttl)
        except RedisError as exc:
            # Fail OPEN: Redis is unavailable, so we cannot tell whether this update is a
            # redelivery. Processing it is the lesser evil — raising here would drop 100% of
            # updates, and in webhook mode would return HTTP 500, which makes Telegram retry
            # and turns a Redis blip into a retry storm. A bot handling payments may prefer
            # fail-closed: re-raise instead.
            logger.warning(f"dedup unavailable, processing without it | update_id: {event.update_id} | error: {exc}")
            return await handler(event, data)

        if not claimed:
            logger.warning(f"duplicate update dropped | update_id: {event.update_id}")
            return None

        return await handler(event, data)
