from __future__ import annotations
import datetime
from typing import TYPE_CHECKING

from aiogram.types import LabeledPrice
from aiogram.utils.i18n import gettext as _
from loguru import logger

from bot.analytics.types import BaseEvent, EventProperties

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import SuccessfulPayment

    from bot.analytics.types import AbstractAnalyticsLogger
    from bot.core.config import PaymentSettings
    from bot.database.repositories import PaymentRepository
    from bot.services.users import UserService

PREMIUM_PAYLOAD_PREFIX = "premium"
# Bot API: `subscription_period` "must always be 2592000 (30 days)" — the only value
# Telegram currently accepts for a recurring Stars subscription.
STARS_SUBSCRIPTION_PERIOD_SECONDS = 2592000


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

    @staticmethod
    def _payload_user_id(payload: str) -> int | None:
        """The user id `build_payload` embedded, or None if the payload is malformed."""
        candidate = payload.removeprefix(f"{PREMIUM_PAYLOAD_PREFIX}:").partition(":")[0]
        return int(candidate) if candidate.isdigit() else None

    async def validate(self, payload: str, payer_id: int, amount: int, currency: str) -> tuple[bool, str | None]:
        """Answer the pre-checkout query. Telegram gives us ~10 seconds.

        Re-check price and currency here rather than trusting the invoice: the
        client controls neither, but a stale invoice from an old price change
        would otherwise be honoured at the old amount.
        """
        if not payload.startswith(f"{PREMIUM_PAYLOAD_PREFIX}:"):
            return False, _("payment error: unknown product")
        # `build_payload` mints the payload for one named user, so a mismatch means a
        # forwarded invoice link. Honouring it credits the payer while `payments.payload`
        # records somebody else — the only field that stores intent, and the one refund
        # and support tooling reads. Refusing is better than a silent miscredit.
        if self._payload_user_id(payload) != payer_id:
            return False, _("payment error: invoice belongs to another account")
        if currency != self._settings.currency:
            return False, _("payment error: unsupported currency")
        if amount != self._settings.premium_price:
            return False, _("payment error: price changed")
        return True, None

    def _expires_at(self, payment: SuccessfulPayment) -> datetime.datetime:
        """When the period this payment bought runs out.

        A one-off Stars invoice carries no `subscription_expiration_date` — Telegram
        only populates it for recurring subscriptions — so without a computed fallback
        the column is always NULL, `active_subscription()` (the only expiry-aware query
        in the codebase) can never match, and the "30 days" the user was sold is
        unbounded in practice. A Telegram-supplied expiry still wins when present.
        """
        supplied = _naive_utc(payment.subscription_expiration_date)
        if supplied is not None:
            return supplied
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        return now + datetime.timedelta(days=self._settings.subscription_period_days)

    async def record(self, user_id: int, payment: SuccessfulPayment) -> bool:
        """Record a payment and credit the user. Returns False if already seen.

        The insert and the premium grant are one transaction: `PaymentRepository.record`
        only flushes, and `UserService.set_premium` -> `UserRepository._update` commits
        the pair on the shared session. Keep that order and do not add a commit between
        them — a durable charge with no grant cannot be retried, because the unique
        charge id rejects every replay as a duplicate.
        """
        row = await self._payments.record(
            user_id=user_id,
            currency=payment.currency,
            amount=payment.total_amount,
            payload=payment.invoice_payload,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
            provider_payment_charge_id=payment.provider_payment_charge_id,
            is_recurring=bool(payment.is_recurring),
            subscription_expires_at=self._expires_at(payment),
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

    async def subscription_link(self, bot: Bot, user_id: int) -> str:
        """A recurring Stars subscription link.

        Note `subscription_period` exists on `create_invoice_link` but NOT on
        `send_invoice` — recurring Stars must go through a link, which the client
        opens with `tg.openInvoice(...)` or which you send as a URL button.
        """
        period = self._settings.subscription_period_days * 86400
        if period != STARS_SUBSCRIPTION_PERIOD_SECONDS:
            # Bot API: `subscription_period` "must always be 2592000 (30 days)".
            # Failing here names the setting; letting it through produces an opaque
            # "Bad Request: invalid subscription period" from Telegram instead.
            msg = (
                f"PAYMENT_SUBSCRIPTION_PERIOD_DAYS must be 30 for Stars subscriptions, "
                f"got {self._settings.subscription_period_days}"
            )
            raise ValueError(msg)

        return await bot.create_invoice_link(
            title="Premium subscription",
            description=f"Renews every {self._settings.subscription_period_days} days",
            payload=self.build_payload(user_id),
            currency=self._settings.currency,
            prices=[LabeledPrice(label="Premium", amount=self._settings.premium_price)],
            subscription_period=period,
        )

    async def refund(self, bot: Bot, user_id: int, charge_id: str) -> bool:
        """Refund a Stars payment and revoke premium.

        Returns False when the charge is unknown to us — refunding a charge we
        never recorded would revoke premium the user paid for elsewhere.
        """
        existing = await self._payments.get_by_charge_id(charge_id)
        if existing is None or existing.user_id != user_id:
            return False

        # Telegram first: the external refund is the irreversible step, so it must not
        # happen behind a local write that could still be rolled back. The two DB writes
        # after it share one transaction (`mark_refunded` flushes, `set_premium` commits),
        # so the row can never be REFUNDED while the user keeps premium.
        #
        # Known seam, deliberately not fixed here: if the Telegram call succeeds and the
        # transaction below then fails, a second `/refund` re-runs `refund_star_payment`
        # on an already-refunded charge, which Telegram rejects — so the admin cannot
        # self-heal through the command and has to fix the row by hand.
        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
        await self._payments.mark_refunded(charge_id)
        await self._users.set_premium(user_id, value=False)
        logger.info(f"payment refunded | user_id: {user_id} | charge: {charge_id}")
        return True
