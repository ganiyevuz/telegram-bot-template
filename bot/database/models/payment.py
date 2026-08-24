# ruff: noqa: TC003  - SQLAlchemy resolves `Mapped[...]` annotations at class-definition
# time via runtime introspection (de-stringifying them against the module's globals), so
# datetime must stay a real module-level import - the same trap as di.py's get_type_hints().
from __future__ import annotations
import datetime
import enum

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.models.base import Base, created_at


class PaymentStatus(enum.StrEnum):
    PAID = "paid"
    REFUNDED = "refunded"


class PaymentModel(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("telegram_payment_charge_id", name="uq_payments_charge_id"),
        # Serves the `still_paid` EXISTS in `PaymentRepository.expire_premium`, which ran
        # hourly as a sequential scan over the whole table. Column order matters:
        # `status` is the equality predicate, `subscription_expires_at` the range one.
        Index("ix_payments_status_expires_at", "status", "subscription_expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)

    currency: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int]
    payload: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.PAID)

    telegram_payment_charge_id: Mapped[str] = mapped_column(String(128))
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(128), default=None)

    is_recurring: Mapped[bool] = mapped_column(default=False)
    subscription_expires_at: Mapped[datetime.datetime | None] = mapped_column(default=None)

    created_at: Mapped[created_at]
