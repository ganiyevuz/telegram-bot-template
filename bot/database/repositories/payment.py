from __future__ import annotations
import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from bot.database.models import PaymentModel, PaymentStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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
        """Insert a payment. Returns None if this charge id was already recorded.

        Mirrors UserRepository.create()'s contract: None means another writer got
        there first, and the session has been rolled back so it stays usable.
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
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return None
        return payment

    async def get_by_charge_id(self, charge_id: str) -> PaymentModel | None:
        query = select(PaymentModel).filter_by(telegram_payment_charge_id=charge_id)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def mark_refunded(self, charge_id: str) -> None:
        stmt = (
            update(PaymentModel)
            .where(PaymentModel.telegram_payment_charge_id == charge_id)
            .values(status=PaymentStatus.REFUNDED)
        )
        await self._session.execute(stmt)
        await self._session.commit()

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
