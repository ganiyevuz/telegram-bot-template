from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from loguru import logger
from redis.exceptions import RedisError

from bot.cache.keys import CacheKeys
from bot.middlewares.metrics import THROTTLED

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

    from bot.core.di import ThrottleBucket


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user rate limiting shared across replicas.

    Keyed by user rather than chat, so members of a group do not consume each
    other's allowance.
    """

    def __init__(self, bucket: ThrottleBucket) -> None:
        self._bucket = bucket
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Never throttle a payment notification. Throttling exists to protect the bot from
        # chat spam, and this is not chat spam: the user cannot send one at will — Telegram
        # emits it only after taking their money — while dropping it means the Stars are
        # gone and premium is never granted. The shipped default (RATE_LIMIT=0.5 -> capacity
        # 2) is emptied by an ordinary /start plus two taps, and `pre_checkout_query` is not
        # throttled at all, so the charge has already happened by the time this arrives.
        # `getattr` because the middleware is typed against `TelegramObject`, not `Message`.
        if getattr(event, "successful_payment", None) is not None:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        try:
            granted = await self._bucket.try_acquire(CacheKeys.throttle("user", user.id))
        except RedisError as exc:
            # Fail OPEN: Redis is unavailable, so we cannot tell whether this user is over
            # their limit. Letting the update through is the lesser evil — raising here
            # would, in webhook mode, return HTTP 500, which makes Telegram retry and turns
            # a Redis blip into a retry storm; in polling mode aiogram would silently drop
            # the update instead. Either way an outage degrades to unthrottled, not silent.
            # A bot enforcing hard quotas (billing, abuse limits) may prefer fail-closed:
            # re-raise instead.
            logger.warning(f"throttle unavailable, processing without it | user_id: {user.id} | error: {exc}")
            return await handler(event, data)

        if not granted:
            logger.debug(f"throttled | user_id: {user.id}")
            THROTTLED.labels(scope="user").inc()
            return None

        return await handler(event, data)
