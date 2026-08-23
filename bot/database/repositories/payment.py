from __future__ import annotations
import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from bot.database.models import PaymentModel, PaymentStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Postgres SQLSTATE for `unique_violation`. Anything else reaching an `IntegrityError`
# handler here (`23503` foreign_key_violation, `23502` not_null_violation, ...) is a real
# failure, not a replay, and must not be reported as one.
UNIQUE_VIOLATION = "23505"


def _is_unique_violation(exc: IntegrityError) -> bool:
    """True only for a duplicate-key failure.

    asyncpg surfaces the SQLSTATE on the driver exception SQLAlchemy wraps; `.orig`
    is reached defensively because a wrapper without it would otherwise turn an
    unrelated integrity failure into a silent "already recorded".
    """
    return getattr(exc.orig, "sqlstate", None) == UNIQUE_VIOLATION


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(  # noqa: PLR0913 - keyword-only fields mirroring SuccessfulPayment; a params object would just move the sprawl
        self,
        *,
        user_id: int,
        currency: str,
        amount: int,
        payload: str,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str | None = None,
        is_recurring: bool = False,
        subscription_expires_at: datetime.datetime | None = None,
    ) -> PaymentModel | None:
        """Insert a payment, without committing. Returns None if this charge id was already recorded.

        Flushes rather than commits: the caller's commit is what makes the row
        durable. `PaymentService.record` credits the user immediately after, through a
        `UserRepository` holding the *same* session (both come from the single REQUEST
        scope `session` provider in bot/core/di.py), so the insert and the premium grant
        land in one transaction. Committing here instead would durably record the charge
        before the user is credited, and `uq_payments_charge_id` would then reject every
        retry as a duplicate — the money taken and the grant unrecoverable.

        None means another writer got there first, and the session has been rolled back
        so it stays usable. Any other integrity failure is re-raised.
        """
        payment = PaymentModel(
            user_id=user_id,
            currency=currency,
            amount=amount,
            payload=payload,
            status=PaymentStatus.PAID,
            telegram_payment_charge_id=telegram_payment_charge_id,
            provider_payment_charge_id=provider_payment_charge_id,
            is_recurring=is_recurring,
            subscription_expires_at=subscription_expires_at,
        )
        self._session.add(payment)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Roll back first either way: the session is unusable until the failed
            # statement is cleared, including on the re-raise path.
            await self._session.rollback()
            if _is_unique_violation(exc):
                return None
            raise
        return payment

    async def get_by_charge_id(self, charge_id: str) -> PaymentModel | None:
        query = select(PaymentModel).filter_by(telegram_payment_charge_id=charge_id)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def mark_refunded(self, charge_id: str) -> None:
        """Flag the charge as refunded, without committing.

        Same contract as `record()`: the caller's commit makes it durable, so
        marking the row and revoking the user's premium are one transaction rather
        than two independently-failing ones.
        """
        stmt = (
            update(PaymentModel)
            .where(PaymentModel.telegram_payment_charge_id == charge_id)
            .values(status=PaymentStatus.REFUNDED)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def active_subscription(self, user_id: int) -> PaymentModel | None:
        """The user's current subscription payment, if one is still in its paid period.

        A row only counts here if it carries an expiry in the future and has not
        been refunded — `mark_refunded` flips `status` but never clears the
        expiry, so both checks are needed.
        """
        # `subscription_expires_at` is a naive TIMESTAMP WITHOUT TIME ZONE, like
        # `created_at` (see base.py) — the whole table is naive-UTC by convention,
        # so the comparison value must be naive too.
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        query = (
            select(PaymentModel)
            .where(
                PaymentModel.user_id == user_id,
                PaymentModel.status == PaymentStatus.PAID,
                PaymentModel.subscription_expires_at.is_not(None),
                PaymentModel.subscription_expires_at > now,
            )
            .order_by(PaymentModel.subscription_expires_at.desc())
            .limit(1)
        )
        return (await self._session.execute(query)).scalar_one_or_none()
