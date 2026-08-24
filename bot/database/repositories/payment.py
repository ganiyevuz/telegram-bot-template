from __future__ import annotations
import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from bot.database.models import PaymentModel, PaymentStatus, UserModel

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

    async def commit(self) -> None:
        """Make this session's flushed writes durable.

        `record()` and `mark_refunded()` deliberately only flush, leaving the commit to
        `UserRepository._update` so the payment row and the premium grant land together.
        `PaymentService.refund` has one branch where it must NOT touch `users` at all —
        the user still holds another live period — and that branch would otherwise leave
        the refund flushed and never committed, i.e. rolled back when the REQUEST scope
        closes. This is that branch's commit; it is not a general-purpose escape hatch.
        """
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

    async def expire_premium(self) -> list[int]:
        """Clear `is_premium` for users whose paid periods have all run out.

        Two statements per run — NOT one, and NOT one per user. A single
        `UPDATE users ... WHERE NOT EXISTS (...)` is wrong under READ COMMITTED: when it
        blocks on a row lock held by a concurrent credit, Postgres re-checks the qual
        against the updated tuple (EvalPlanQual) but re-runs the `EXISTS` subplan against
        the statement's ORIGINAL snapshot — which does not contain the payment row that
        credit just committed. The sweep then clears a user who paid seconds ago, and
        nothing re-grants: this job only ever clears, and `uq_payments_charge_id` makes
        the credit unreplayable.

        So: lock the candidates first with `SKIP LOCKED`, then update only those ids.
        - A user being credited right now holds the lock on their row (`PaymentService`
          flushes the payment, then UPDATEs `users`), so `SKIP LOCKED` passes over them
          entirely and the next hourly run re-evaluates them against a snapshot that
          includes their payment.
        - A user whose credit committed between this statement's snapshot and its visit
          to their row is caught by repeating `~still_paid` on the UPDATE: that is a new
          statement, so it takes a fresh snapshot, and the rows are already locked by us
          so nothing can have changed under it.
        - `ever_paid` keeps the sweep off accounts this system never granted premium to.
          `admin/views/users.py` has `can_edit = True` with `is_premium` in its column
          list, so the Flask panel is a legitimate writer for this column; a comped
          tester, partner or support gesture has no `payments` row and was not granted by
          us, so revoking it is not ours to do.

        Returns the cleared user ids (rather than a bare count) so the caller can
        invalidate each user's cache entry.
        """
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        still_paid = (
            select(PaymentModel.id)
            .where(
                PaymentModel.user_id == UserModel.id,
                PaymentModel.status == PaymentStatus.PAID,
                PaymentModel.subscription_expires_at.is_not(None),
                PaymentModel.subscription_expires_at > now,
            )
            .exists()
        )
        ever_paid = select(PaymentModel.id).where(PaymentModel.user_id == UserModel.id).exists()
        # `of=UserModel` so the lock covers only the rows we are about to update —
        # without it Postgres would also lock every `payments` row the EXISTS subplans
        # touched, blocking concurrent credits instead of stepping around them.
        candidates = (
            select(UserModel.id)
            .where(UserModel.is_premium.is_(True), ever_paid, ~still_paid)
            .with_for_update(of=UserModel, skip_locked=True)
        )
        locked = list((await self._session.execute(candidates)).scalars())
        if not locked:
            await self._session.commit()
            return []

        stmt = (
            update(UserModel)
            .where(UserModel.id.in_(locked), ~still_paid)
            .values(is_premium=False)
            .returning(UserModel.id)
        )
        expired = list((await self._session.execute(stmt)).scalars())
        await self._session.commit()
        return expired
