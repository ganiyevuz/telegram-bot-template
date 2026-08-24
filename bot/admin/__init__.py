from __future__ import annotations
from typing import TYPE_CHECKING

from sqladmin import Admin

from bot.admin.auth import SESSION_KEY, SUPERUSER_SESSION_KEY, AdminAuth, seed_default_admin
from bot.admin.dashboard import DashboardView
from bot.admin.security import hash_password, verify_password
from bot.admin.views import AdminAdmin, PaymentAdmin, RoleAdmin, UserAdmin
from bot.core.config import BOT_DIR

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from bot.core.config import Settings

__all__ = [
    "SESSION_KEY",
    "SUPERUSER_SESSION_KEY",
    "AdminAuth",
    "hash_password",
    "seed_default_admin",
    "setup_admin",
    "verify_password",
]

# `dashboard.html` lives here; SQLAdmin's own templates stay reachable under the
# `sqladmin/` prefix its PackageLoader adds, which is what lets the dashboard extend
# `sqladmin/layout.html`.
TEMPLATES_DIR = str(BOT_DIR / "admin" / "templates")


def _promote_dashboard_to_index(admin: Admin) -> None:
    """Make `DashboardView` answer the panel root instead of SQLAdmin's blank index.

    `Admin.__init__` assigns `self.admin.router.routes = [... Route("/", self.index,
    name="index") ...]` before any view can be registered, and `add_base_view` only
    *appends* to that same list. Starlette returns the first route that matches, so
    `DashboardView`'s own `@expose("/")` route is registered but unreachable — a bare
    GET on the base URL renders SQLAdmin's bundled empty index instead.

    Reordering rather than replacing: `admin:index` is the route name SQLAdmin's own
    layout uses for the navbar brand and for the redirect after a successful login, so
    dropping that route would break every page that renders the layout. Moving the
    dashboard ahead of it leaves the name resolvable and both paths identical.
    """
    routes = admin.admin.router.routes
    names = [getattr(route, "name", "") for route in routes]
    dashboard = names.index(f"view-{DashboardView.identity}")
    index = names.index("index")
    routes.insert(index, routes.pop(dashboard))


def setup_admin(
    app: FastAPI,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Mount the panel on `app` at `/admin`.

    Called from the API lifespan rather than from `create_app()`: the engine and the
    sessionmaker are APP-scoped dishka objects that do not exist until the container is
    built. Mounting there is safe — `Admin.__init__` ends in `app.mount(...)`, which
    appends to a route list Starlette re-reads on every request, unlike `add_middleware`,
    which is what forced the `ContainerMiddleware` split in `bot/entrypoints/api.py`.
    The login cookie's `SessionMiddleware` is not affected either: SQLAdmin builds its
    own Starlette sub-application and `AuthenticationBackend.__init__` installs that
    middleware into *its* stack, which is compiled lazily on its first request.
    """
    admin = Admin(
        app=app,
        engine=engine,
        # `session_maker`, not `sessionmaker` — SQLAdmin's own model views open sessions
        # from it directly, outside any dishka scope. It is the container's, so the panel
        # shares the one engine and connection pool the bot already runs on. `Admin`
        # re-`configure()`s it with `autoflush=False, autocommit=False`; the container
        # already builds it with the former and the latter is the default, so nothing the
        # bot resolves later changes behaviour.
        session_maker=sessionmaker,
        base_url="/admin",
        title="Telegram Bot",
        templates_dir=TEMPLATES_DIR,
        authentication_backend=AdminAuth(settings.admin.secret_key.get_secret_value(), sessionmaker),
    )
    # `add_base_view`, not `add_view`: `DashboardView` is a `BaseView`, and this is the
    # call that assigns `view.templates` before instantiating it.
    admin.add_base_view(DashboardView)
    admin.add_view(UserAdmin)
    admin.add_view(PaymentAdmin)
    admin.add_view(AdminAdmin)
    admin.add_view(RoleAdmin)
    _promote_dashboard_to_index(admin)
