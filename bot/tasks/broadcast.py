from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from dishka import Scope
from loguru import logger
from redis.asyncio import Redis

from bot.cache.keys import CacheKeys
from bot.database.repositories import UserRepository
from bot.notifier import AlertLevel, NotifierService
from bot.services.users import UserService
from bot.tasks import broker, get_container

CHUNK_SIZE = 500
PROGRESS_TTL = 86_400


async def _report_completion(redis: Redis, broadcast_id: str) -> None:
    """Alert once, when the last chunk of a broadcast has been processed.

    Called from both ends of the fan-out, because either can be the one that finishes
    last. `queued` is written only after `start_broadcast` has enqueued everything, so a
    chunk completing mid-fan-out correctly reads the broadcast as unfinished — but a
    small audience can be delivered in full before that write lands, which is why
    `start_broadcast` re-checks straight after making it. `hsetnx` is what makes two
    callers safe: the first to claim the flag alerts, everyone else — a second worker
    finishing its own chunk at the same instant included — gets 0 and returns.

    Never raises. It runs at the tail of a task whose retry would re-send the entire
    chunk, so a failed alert must not be what triggers duplicate messages to 500 users.
    """
    key = CacheKeys.broadcast(broadcast_id)
    try:
        # This client is not in decode_responses mode, so the field names arrive as
        # bytes; the isinstance guard keeps this correct if that ever changes.
        raw = await redis.hgetall(key)
        progress = {(f.decode() if isinstance(f, bytes) else f): int(v) for f, v in raw.items()}
        total = progress.get("queued")
        if total is None:
            return
        if progress.get("sent", 0) + progress.get("blocked", 0) + progress.get("failed", 0) < total:
            return
        if not await redis.hsetnx(key, "alerted", 1):
            return
        notifier = await get_container().get(NotifierService)
        await notifier.send(
            AlertLevel.INFO,
            "broadcast complete",
            f"id: {broadcast_id}\n"
            f"recipients: {total}\n"
            f"sent: {progress.get('sent', 0)}\n"
            f"blocked: {progress.get('blocked', 0)}\n"
            f"failed: {progress.get('failed', 0)}",
            "broadcast:complete",
        )
    except Exception as exc:  # noqa: BLE001 - alerting must not become a source of errors
        logger.warning(f"broadcast completion alert failed | id: {broadcast_id} | {type(exc).__name__}: {exc}")


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

    # Written last, and deliberately so: its presence is what tells a finishing chunk
    # that no further chunks are coming. See `_report_completion`.
    await redis.hset(CacheKeys.broadcast(broadcast_id), "queued", queued)
    logger.info(f"broadcast queued | id: {broadcast_id} | recipients: {queued}")
    await _report_completion(redis, broadcast_id)
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
    await _report_completion(redis, broadcast_id)
