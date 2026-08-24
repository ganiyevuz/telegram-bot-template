from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.models.base import Base, created_at

# Association table, not a model: it carries no columns of its own beyond the two
# foreign keys, so there is nothing to query it by directly.
roles_admins = Table(
    "roles_admins",
    Base.metadata,
    Column("admin_id", ForeignKey("admin.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("role.id", ondelete="CASCADE"), primary_key=True),
)


class RoleModel(Base):
    __tablename__ = "role"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str | None] = mapped_column(String(255), default=None)

    repr_cols = ("name",)

    def __str__(self) -> str:
        return self.name


class AdminModel(Base):
    """A panel operator. Deliberately NOT `UserModel` — that is a Telegram user.

    Two separate populations with nothing in common: a Telegram user is identified by
    a Telegram id and never has a password; an admin has an email and a password and
    never has a Telegram id.
    """

    __tablename__ = "admin"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str | None] = mapped_column(String(255), default=None)
    last_name: Mapped[str | None] = mapped_column(String(255), default=None)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    # The scrypt digest, never the password. See bot/admin/security.py.
    password: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[created_at]

    roles: Mapped[list[RoleModel]] = relationship(secondary=roles_admins, lazy="selectin")

    repr_cols = ("email",)

    def __str__(self) -> str:
        return self.email
