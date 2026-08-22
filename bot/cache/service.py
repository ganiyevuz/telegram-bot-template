from __future__ import annotations
from typing import TYPE_CHECKING, Any, TypeVar

import orjson
from loguru import logger

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from redis.asyncio import Redis

T = TypeVar("T", bound=bool | int | float | str | list[Any] | dict[str, Any])

DEFAULT_TTL = 60


class CacheService:
    """Cache-aside helper over Redis using orjson.

    Pickle is deliberately not supported: values are deserialized from Redis,
    and `pickle.loads` on attacker-controlled bytes is remote code execution.
    Only JSON-representable values may be cached.
    """

    def __init__(self, redis: Redis, default_ttl: int = DEFAULT_TTL) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    async def get(self, key: str, type_: type[T]) -> T | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            value = orjson.loads(raw)
        except orjson.JSONDecodeError:
            logger.warning(f"discarding undecodable cache entry | key: {key}")
            await self._redis.delete(key)
            return None
        if not isinstance(value, type_):
            logger.warning(f"cache type mismatch | key: {key} | want: {type_.__name__}")
            await self._redis.delete(key)
            return None
        return value

    async def set(self, key: str, value: T, ttl: int | None = None) -> None:
        await self._redis.set(key, orjson.dumps(value), ex=ttl or self._default_ttl)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._redis.delete(*keys)

    async def invalidate_user(self, user_id: int) -> None:
        await self.delete(*CacheKeys.for_user(user_id), CacheKeys.user_count())
