# ruff: noqa: TC002 - SQLAdmin and Starlette resolve these at runtime
from __future__ import annotations

from loguru import logger
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse

from bot.admin.security import hash_password, verify_password
from bot.core.config import SHIPPED_ADMIN_PASSWORD, Settings
from bot.database.models import AdminModel, RoleModel

SESSION_KEY = "admin_email"
# Read by Task 3's SuperuserOnly mixin to gate the Roles and Admins views.
SUPERUSER_SESSION_KEY = "admin_is_superuser"
SUPERUSER_ROLE = "superuser"

DEFAULT_ROLES = (
    ("user", "does not have access to other administrators"),
    (SUPERUSER_ROLE, "has access to manage all administrators"),
)


class AdminAuth(AuthenticationBackend):
    """Session login for the panel.

    Holds a sessionmaker rather than a session: SQLAdmin calls these methods per
    request, outside any dishka REQUEST scope, so each one opens and closes its own.
    """

    def __init__(self, secret_key: str, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        if not secret_key:
            # `AdminSettings.secret_key` ships empty on purpose. Refusing here means an
            # unset ADMIN_SECRET_KEY cannot degrade into a panel whose session cookies
            # are signed with the empty string — which anybody could forge.
            msg = "ADMIN_SECRET_KEY must be set — it signs the admin session cookie"
            raise ValueError(msg)
        super().__init__(secret_key=secret_key)
        self._sessionmaker = sessionmaker

    async def login(self, request: Request) -> bool:
        form = await request.form()
        email = str(form.get("username", ""))
        password = str(form.get("password", ""))

        async with self._sessionmaker() as session:
            admin = (await session.execute(select(AdminModel).filter_by(email=email))).scalar_one_or_none()

        # Verify even when the row is missing, against a throwaway hash, so a request
        # for an unknown email costs the same time as one for a known email. Returning
        # early here would turn the panel into an account-enumeration oracle.
        stored = admin.password if admin is not None else "scrypt$00$00"
        ok = verify_password(password, stored)
        if not ok or admin is None or not admin.active:
            logger.warning(f"admin login rejected | email: {email}")
            return False

        request.session.update(
            {
                SESSION_KEY: admin.email,
                SUPERUSER_SESSION_KEY: any(role.name == SUPERUSER_ROLE for role in admin.roles),
            },
        )
        logger.info(f"admin login | email: {admin.email}")
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool | RedirectResponse:
        email = request.session.get(SESSION_KEY)
        if not email:
            return False
        # Re-read on every request rather than trusting the cookie: deactivating an
        # admin must take effect immediately, not whenever their session happens to
        # expire.
        async with self._sessionmaker() as session:
            admin = (await session.execute(select(AdminModel).filter_by(email=email))).scalar_one_or_none()
        if admin is None or not admin.active:
            return False
        # Same reason, for the role: revoking `superuser` must close the Roles and
        # Admins views on the next request, not at session expiry.
        request.session[SUPERUSER_SESSION_KEY] = any(role.name == SUPERUSER_ROLE for role in admin.roles)
        return True


async def seed_default_admin(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """Create the two roles and one superuser, once, on an empty admin table.

    Guarded on the table being EMPTY, not on the default email being absent: keying it
    on the email would re-create the default admin every restart after someone deleted
    it, which is a backdoor rather than a convenience.
    """
    async with sessionmaker() as session:
        if await session.scalar(select(AdminModel.id).limit(1)) is not None:
            return

        names = [name for name, _ in DEFAULT_ROLES]
        found = (await session.scalars(select(RoleModel).filter(RoleModel.name.in_(names)))).all()
        existing = {role.name: role for role in found}
        roles = []
        for name, description in DEFAULT_ROLES:
            role = existing.get(name) or RoleModel(name=name, description=description)
            session.add(role)
            roles.append(role)

        password = settings.admin.default_password.get_secret_value()
        session.add(
            AdminModel(
                first_name="Admin",
                email=settings.admin.default_email,
                password=hash_password(password),
                roles=roles,
            ),
        )
        try:
            await session.commit()
        except IntegrityError:
            # Two processes booting against the same database both saw an empty table.
            # The unique constraint on `admin.email` settles it; the loser has nothing
            # to do.
            await session.rollback()
            logger.info("default admin already seeded by another process")
            return

    logger.info(f"seeded default admin | email: {settings.admin.default_email}")
    if password == SHIPPED_ADMIN_PASSWORD:
        # Plain `==`, not compare_digest: this compares configuration against a value
        # printed in .env.example, with no attacker able to time it.
        logger.warning(
            f"default admin is using the password this template ships with "
            f"— change DEFAULT_ADMIN_PASSWORD and the password of {settings.admin.default_email}",
        )
