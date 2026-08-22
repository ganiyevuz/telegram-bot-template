# ruff: noqa: TC002, TC003  - these annotations are resolved at runtime (dishka inspects
# provider method signatures via get_type_hints), so the imports must stay at module level
from __future__ import annotations
from collections.abc import AsyncIterable

from aiogram import Bot
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n.core import I18n
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide
from redis.asyncio import ConnectionPool, Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.cache.service import CacheService
from bot.core.config import DEFAULT_LOCALE, I18N_DOMAIN, LOCALES_DIR, Settings
from bot.database.repositories import UserRepository
from bot.services.users import UserService
from bot.telegram.factory import create_bot


class AppProvider(Provider):
    """Process-lifetime objects. Built once, torn down on shutdown."""

    scope = Scope.APP

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    @provide
    def settings(self) -> Settings:
        return self._settings

    @provide
    async def redis(self, settings: Settings) -> AsyncIterable[Redis]:
        pool = ConnectionPool.from_url(settings.redis.url)
        client = Redis(connection_pool=pool)
        yield client
        await client.aclose()
        await pool.aclose()

    @provide
    async def engine(self, settings: Settings) -> AsyncIterable[AsyncEngine]:
        engine = create_async_engine(
            url=settings.db.url,
            echo=settings.debug,
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.max_overflow,
            pool_pre_ping=True,
            # pgbouncer runs in transaction pooling mode, where server-side
            # prepared statements cannot be reused across transactions.
            connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
        )
        yield engine
        await engine.dispose()

    @provide
    def sessionmaker(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @provide
    async def bot(self, settings: Settings) -> AsyncIterable[Bot]:
        bot = create_bot(settings)
        yield bot
        await bot.session.close()

    @provide
    async def storage(self, redis: Redis) -> AsyncIterable[RedisStorage]:
        storage = RedisStorage(redis=redis, key_builder=DefaultKeyBuilder(with_bot_id=True))
        yield storage
        await storage.close()

    @provide
    def i18n(self) -> I18n:
        return I18n(path=LOCALES_DIR, default_locale=DEFAULT_LOCALE, domain=I18N_DOMAIN)

    @provide
    def cache(self, redis: Redis) -> CacheService:
        return CacheService(redis)


class RequestProvider(Provider):
    """Per-update objects. The session is created only if something resolves it."""

    scope = Scope.REQUEST

    @provide
    async def session(self, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterable[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    @provide
    def users(self, session: AsyncSession) -> UserRepository:
        return UserRepository(session)

    @provide
    def user_service(self, users: UserRepository, cache: CacheService) -> UserService:
        return UserService(users, cache)


def create_container(settings: Settings) -> AsyncContainer:
    return make_async_container(AppProvider(settings), RequestProvider())
