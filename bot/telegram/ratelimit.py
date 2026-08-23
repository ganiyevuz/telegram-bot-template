from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramRetryAfter
from loguru import logger

from bot.cache.keys import CacheKeys
from bot.middlewares.metrics import OUTBOUND_RETRY_AFTER, OUTBOUND_WAITS

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
    from aiogram.methods import TelegramMethod

    from bot.cache.ratelimit import TokenBucket

MAX_RETRY_AFTER = 60


class OutboundRateLimiter:
    """Paces every outgoing Bot API call against shared Redis buckets.

    Registered on the session, so it applies to API replicas and workers alike —
    including sends made from inside a broadcast task.
    """

    def __init__(self, global_bucket: TokenBucket, chat_bucket: TokenBucket) -> None:
        self._global = global_bucket
        self._chat = chat_bucket

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        chat_id = getattr(method, "chat_id", None)

        await self._wait(self._global, CacheKeys.outbound("global", bot.id), "global")
        if chat_id is not None:
            await self._wait(self._chat, CacheKeys.outbound("chat", chat_id), "chat")

        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as exc:
            OUTBOUND_RETRY_AFTER.inc()
            delay = min(exc.retry_after, MAX_RETRY_AFTER)
            logger.warning(f"telegram 429 | sleeping {delay}s | method: {type(method).__name__}")
            await asyncio.sleep(delay)
            return await make_request(bot, method)

    @staticmethod
    async def _wait(bucket: TokenBucket, key: str, scope: str) -> None:
        while (delay := await bucket.acquire(key)) > 0:
            OUTBOUND_WAITS.labels(scope=scope).inc()
            await asyncio.sleep(delay)
