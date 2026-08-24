from __future__ import annotations

import orjson
from loguru import logger
from redis.asyncio import Redis

from bot.analytics.amplitude import AmplitudeTelegramLogger
from bot.core.config import Settings
from bot.tasks import broker, get_container

BATCH = 500


@broker.task(task_name="analytics:flush", schedule=[{"cron": "* * * * *"}])
async def flush_analytics() -> int:
    """Drain the buffer and ship it. Scheduled every minute."""
    container = get_container()
    settings = await container.get(Settings)
    redis = await container.get(Redis)

    if not settings.analytics.amplitude_api_key:
        return 0

    key = settings.analytics.buffer_key
    raw = await redis.rpop(key, BATCH)
    if not raw:
        return 0
    payloads = [raw] if isinstance(raw, bytes) else raw

    sink = AmplitudeTelegramLogger(api_token=settings.analytics.amplitude_api_key)
    sent = 0
    for payload in payloads:
        try:
            await sink.send_raw(orjson.loads(payload))
            sent += 1
        except Exception as exc:  # noqa: BLE001 - one bad event must not stall the batch
            logger.warning(f"analytics event dropped | error: {exc}")
    logger.info(f"analytics flushed | sent: {sent}")
    return sent
