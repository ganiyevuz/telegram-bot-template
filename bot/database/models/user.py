from __future__ import annotations

from sqlalchemy import Index, text
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.models.base import Base, big_int_pk, created_at


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        # PARTIAL, on purpose: the only query that reads `is_premium` as a predicate is
        # the hourly `expire_premium` sweep, which looks exclusively at premium users —
        # a small minority of the table. Indexing just those rows keeps the index tiny
        # and costs nothing on the overwhelmingly common non-premium write path.
        Index("ix_users_premium", "id", postgresql_where=text("is_premium IS TRUE")),
    )

    id: Mapped[big_int_pk]
    first_name: Mapped[str]
    last_name: Mapped[str | None]
    username: Mapped[str | None]
    language_code: Mapped[str | None]
    referrer: Mapped[str | None]
    created_at: Mapped[created_at]

    is_admin: Mapped[bool] = mapped_column(default=False)
    is_suspicious: Mapped[bool] = mapped_column(default=False)
    is_block: Mapped[bool] = mapped_column(default=False)
    # "Has paid US for premium" — NOT the caller's Telegram Premium subscription
    # (`aiogram.types.User.is_premium` / `WebAppUser.is_premium`), which says nothing
    # about whether they bought anything here. Seeding it from that field at
    # registration marked every Telegram Premium subscriber as a paying customer and
    # silently hid the Mini App's Subscribe button from them. Written by
    # `PaymentService` (grant/revoke), by the hourly `payments:expire_premium` sweep,
    # and by hand in the Flask admin panel — never by registration.
    is_premium: Mapped[bool] = mapped_column(default=False)
