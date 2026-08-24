from __future__ import annotations
from typing import TYPE_CHECKING

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from aiogram.types import User as TgUser

    from bot.cache.service import CacheService
    from bot.database.repositories import UserRepository

USER_TTL = 300
COUNT_TTL = 60


class UserService:
    """Use cases over users. Owns cache invalidation so call sites cannot forget it."""

    def __init__(self, users: UserRepository, cache: CacheService) -> None:
        self._users = users
        self._cache = cache

    async def ensure_registered(self, tg_user: TgUser, referrer: str | None) -> bool:
        """Register the user if new. Returns True when a row was created.

        `exists()` then `create()` is a check-then-act race: on concurrent
        first contact (e.g. two replicas handling near-simultaneous updates
        for the same new user) more than one caller can pass the `exists`
        check before either has committed. `UserRepository.create()` lets
        Postgres's primary key be the single source of truth and reports
        back which side lost the race, so only the winner returns True.
        """
        key = CacheKeys.user_exists(tg_user.id)
        if await self._cache.get(key, bool):
            return False
        if await self._users.exists(tg_user.id):
            await self._cache.set(key, value=True, ttl=USER_TTL)
            return False

        created = await self._users.create(tg_user, referrer)
        if created is None:
            # Lost the race: another request already inserted this user.
            await self._cache.set(key, value=True, ttl=USER_TTL)
            return False

        await self._cache.invalidate_user(tg_user.id)
        await self._cache.set(key, value=True, ttl=USER_TTL)
        return True

    async def language_of(self, user_id: int) -> str:
        key = CacheKeys.user_language(user_id)
        cached = await self._cache.get(key, str)
        if cached is not None:
            return cached
        value = await self._users.language_of(user_id) or ""
        await self._cache.set(key, value, ttl=USER_TTL)
        return value

    async def set_language(self, user_id: int, language_code: str) -> None:
        await self._users.set_language(user_id, language_code)
        await self._cache.delete(CacheKeys.user_language(user_id))

    async def first_name_of(self, user_id: int) -> str:
        key = CacheKeys.user_first_name(user_id)
        cached = await self._cache.get(key, str)
        if cached is not None:
            return cached
        value = await self._users.first_name_of(user_id) or ""
        await self._cache.set(key, value, ttl=USER_TTL)
        return value

    async def is_admin(self, user_id: int) -> bool:
        key = CacheKeys.user_is_admin(user_id)
        cached = await self._cache.get(key, bool)
        if cached is not None:
            return cached
        value = await self._users.is_admin(user_id)
        await self._cache.set(key, value, ttl=USER_TTL)
        return value

    async def set_admin(self, user_id: int, *, value: bool) -> None:
        await self._users.set_admin(user_id, value=value)
        await self._cache.delete(CacheKeys.user_is_admin(user_id))

    async def is_premium(self, user_id: int) -> bool:
        """Deliberately UNCACHED, unlike every other getter here.

        Cache-aside inverts under the Mini App's own refresh: `index.html` calls
        `load()` from the `tg.openInvoice` "paid" callback, which fires when the CLIENT
        confirms payment — before the bot has received the `successful_payment` update.
        That read therefore fetches `false` from Postgres, `set_premium` invalidates
        while it is in flight, and the read then writes its stale `false` back, pinning
        it for the full USER_TTL. The page says "Not subscribed" to someone who has just
        paid, for five minutes, with no event left to correct it.

        It is a single-row primary-key lookup, called about once per Mini App page load.
        The cache bought almost nothing and cost a correctness bug on the one value where
        being wrong is most visible to a paying user. `CacheKeys.user_is_premium` was
        removed with it rather than left orphaned.
        """
        return await self._users.is_premium(user_id)

    async def set_premium(self, user_id: int, *, value: bool) -> None:
        # No invalidation: `is_premium()` above reads straight through to Postgres.
        await self._users.set_premium(user_id, value=value)

    async def mark_blocked(self, user_id: int, *, value: bool) -> None:
        await self._users.set_blocked(user_id, value=value)
        await self._cache.invalidate_user(user_id)

    async def count(self) -> int:
        key = CacheKeys.user_count()
        cached = await self._cache.get(key, int)
        if cached is not None:
            return cached
        value = await self._users.count()
        await self._cache.set(key, value, ttl=COUNT_TTL)
        return value
