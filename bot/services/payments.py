from __future__ import annotations
import datetime
from typing import TYPE_CHECKING

from loguru import logger

from bot.analytics.types import BaseEvent, EventProperties

if TYPE_CHECKING:
    from aiogram.types import SuccessfulPayment

    from bot.analytics.types import AbstractAnalyticsLogger
    from bot.core.config import PaymentSettings
    from bot.database.repositories import PaymentRepository
    from bot.services.users import UserService

PREMIUM_PAYLOAD_PREFIX = "premium"


def _naive_utc(unix_time: int | None) -> datetime.datetime | None:
    """Telegram sends `subscription_expiration_date` as a Unix timestamp.

    The `payments` table stores naive-UTC timestamps by convention (see
    `PaymentRepository.active_subscription`'s comment on the same point), so the
    conversion happens here rather than pushing a tz-aware value into the repository.
    """
    if unix_time is None:
        return None
    return datetime.datetime.fromtimestamp(unix_time, tz=datetime.UTC).replace(tzinfo=None)


class PaymentService:
    """Owns payment validation and recording, so handlers stay thin.

    `record()` is the only path that credits a user, and it is idempotent by
    construction: the repository's unique charge id decides whether this is a
    first sighting or a replay.
    """

    def __init__(
        self,
        payments: PaymentRepository,
        users: UserService,
        analytics: AbstractAnalyticsLogger,
        settings: PaymentSettings,
    ) -> None:
        self._payments = payments
        self._users = users
        self._analytics = analytics
        self._settings = settings

    def build_payload(self, user_id: int) -> str:
        return f"{PREMIUM_PAYLOAD_PREFIX}:{user_id}:{self._settings.subscription_period_days}d"

    async def validate(self, payload: str, amount: int, currency: str) -> tuple[bool, str | None]:
        """Answer the pre-checkout query. Telegram gives us ~10 seconds.

        Re-check price and currency here rather than trusting the invoice: the
        client controls neither, but a stale invoice from an old price change
        would otherwise be honoured at the old amount.
        """
        if not payload.startswith(f"{PREMIUM_PAYLOAD_PREFIX}:"):
            return False, "Unknown product."
        if currency != self._settings.currency:
            return False, "Unsupported currency."
        if amount != self._settings.premium_price:
            return False, "Price has changed, please reopen the bot and try again."
        return True, None

    async def record(self, user_id: int, payment: SuccessfulPayment) -> bool:
        """Record a payment and credit the user. Returns False if already seen."""
        row = await self._payments.record(
            user_id=user_id,
            currency=payment.currency,
            amount=payment.total_amount,
            payload=payment.invoice_payload,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
            provider_payment_charge_id=payment.provider_payment_charge_id,
            is_recurring=bool(payment.is_recurring),
            subscription_expires_at=_naive_utc(payment.subscription_expiration_date),
        )
        if row is None:
            logger.info(f"duplicate payment ignored | charge: {payment.telegram_payment_charge_id}")
            return False

        await self._users.set_premium(user_id, value=True)
        await self._analytics.log_event(
            BaseEvent(
                user_id=user_id,
                event_type="Complete Purchase",
                revenue=payment.total_amount,
                event_properties=EventProperties(payment_method="Stars"),
            ),
        )
        logger.info(f"payment recorded | user_id: {user_id} | amount: {payment.total_amount}")
        return True
