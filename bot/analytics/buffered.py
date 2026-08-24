from __future__ import annotations
from typing import TYPE_CHECKING

import orjson

from bot.analytics.types import AbstractAnalyticsLogger

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from bot.analytics.types import BaseEvent

MAX_BUFFER = 100_000


class BufferedAnalyticsLogger(AbstractAnalyticsLogger):
    """Pushes events onto a Redis list instead of calling the provider inline.

    The old implementation awaited an Amplitude POST before the handler ran and
    raised on a non-200, so a provider outage stopped the bot. This costs one
    Redis round trip and cannot fail the handler.
    """

    def __init__(self, redis: Redis, key: str, max_buffer: int = MAX_BUFFER) -> None:
        self._redis = redis
        self._key = key
        self._max_buffer = max_buffer

    async def log_event(self, event: BaseEvent) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            await pipe.lpush(self._key, orjson.dumps(event.to_dict()))
            await pipe.ltrim(self._key, 0, self._max_buffer - 1)
            await pipe.execute()
