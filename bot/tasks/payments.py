from __future__ import annotations

from aiogram import Bot
from dishka import Scope
from loguru import logger

from bot.cache.service import CacheService
from bot.database.repositories import PaymentRepository
from bot.tasks import broker, get_container

BATCH = 100


@broker.task(task_name="payments:reconcile", schedule=[{"cron": "*/15 * * * *"}])
async def reconcile_star_payments() -> int:
    """Find Stars charges Telegram recorded that we never did.

    A webhook can return 200 while the update is dropped — a Redis outage does
    exactly that — so a payment can exist on Telegram's side and not on ours.
    This finds those gaps rather than waiting for a user to complain.
    """
    container = get_container()
    bot = await container.get(Bot)

    missing = 0
    transactions = await bot.get_star_transactions(offset=0, limit=BATCH)
    async with container(scope=Scope.REQUEST) as request_container:
        payments = await request_container.get(PaymentRepository)
        for tx in transactions.transactions:
            # `source` is set for incoming payments; outgoing refunds have `receiver`.
            if tx.source is None:
                continue
            if await payments.get_by_charge_id(tx.id) is None:
                missing += 1
                logger.warning(
                    f"star transaction not recorded locally | id: {tx.id} | amount: {tx.amount}",
                )

    if missing:
        logger.error(f"reconciliation found {missing} unrecorded Stars payments")
    return missing


@broker.task(task_name="payments:expire_premium", schedule=[{"cron": "7 * * * *"}])
async def expire_premium() -> int:
    """Clear `is_premium` for users whose paid period has run out.

    Scheduled hourly at minute 7, not on the hour, so this doesn't pile onto
    every other cron job's top-of-hour tick.
    """
    container = get_container()
    async with container(scope=Scope.REQUEST) as request_container:
        payments = await request_container.get(PaymentRepository)
        cache = await request_container.get(CacheService)
        expired_ids = await payments.expire_premium()
        for user_id in expired_ids:
            await cache.invalidate_user(user_id)

    if expired_ids:
        logger.info(f"premium expired | count: {len(expired_ids)}")
    return len(expired_ids)
