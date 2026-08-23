from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from dishka import Scope
from loguru import logger
from redis.asyncio import Redis

from bot.cache.keys import CacheKeys
from bot.database.repositories import UserRepository
from bot.services.users import UserService
from bot.tasks import broker, get_container

CHUNK_SIZE = 500
PROGRESS_TTL = 86_400


@broker.task(task_name="broadcast:start")
async def start_broadcast(broadcast_id: str, text: str, initiator_id: int) -> int:
    """Page the audience and enqueue chunks. Holds at most CHUNK_SIZE ids in memory."""
    container = get_container()
    redis = await container.get(Redis)
    await redis.hset(CacheKeys.broadcast(broadcast_id), mapping={"sent": 0, "blocked": 0, "failed": 0})
    await redis.expire(CacheKeys.broadcast(broadcast_id), PROGRESS_TTL)

    queued = 0
    batch: list[int] = []
    async with container(scope=Scope.REQUEST) as request_container:
        users = await request_container.get(UserRepository)
        async for user in users.stream(batch_size=CHUNK_SIZE):
            if user.is_block:
                continue
            batch.append(user.id)
            if len(batch) >= CHUNK_SIZE:
                await send_chunk.kiq(broadcast_id, batch, text, initiator_id)
                queued += len(batch)
                batch = []
        if batch:
            await send_chunk.kiq(broadcast_id, batch, text, initiator_id)
            queued += len(batch)

    logger.info(f"broadcast queued | id: {broadcast_id} | recipients: {queued}")
    return queued


@broker.task(task_name="broadcast:chunk", retry_on_error=True, max_retries=3)
async def send_chunk(broadcast_id: str, user_ids: list[int], text: str, initiator_id: int) -> None:  # noqa: ARG001 - initiator_id kept for audit/log correlation across the fan-out
    """Send one chunk. Outbound pacing is handled by the session middleware (Task 15)."""
    container = get_container()
    bot = await container.get(Bot)
    redis = await container.get(Redis)
    key = CacheKeys.broadcast(broadcast_id)

    async with container(scope=Scope.REQUEST) as request_container:
        service = await request_container.get(UserService)
        for user_id in user_ids:
            try:
                await bot.send_message(chat_id=user_id, text=text)
            except TelegramForbiddenError, TelegramNotFound:
                await service.mark_blocked(user_id, value=True)
                await redis.hincrby(key, "blocked", 1)
            except Exception as exc:  # noqa: BLE001 - one bad recipient must not kill the chunk
                logger.warning(f"broadcast send failed | user_id: {user_id} | error: {exc}")
                await redis.hincrby(key, "failed", 1)
            else:
                await redis.hincrby(key, "sent", 1)

    progress = await redis.hgetall(key)
    logger.info(f"broadcast chunk done | id: {broadcast_id} | progress: {progress}")
