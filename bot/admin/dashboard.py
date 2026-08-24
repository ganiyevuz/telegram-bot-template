# ruff: noqa: TC002 - SQLAdmin resolves the view signature at runtime
from __future__ import annotations
import datetime

from dishka import AsyncContainer, Scope
from sqladmin import BaseView, expose
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from bot.database.models import UserModel

NEW_USER_WINDOW_DAYS = 1


class DashboardView(BaseView):
    name = "Dashboard"
    icon = "fa-solid fa-chart-line"

    @expose("/", methods=["GET"])
    async def index(self, request: Request) -> Response:
        since = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(
            days=NEW_USER_WINDOW_DAYS,
        )
        # Same reasoning as `PaymentAdmin.refund_payments` in views.py: SQLAdmin mounts
        # its own Starlette sub-app, so `request.app` here is that sub-app rather than
        # the FastAPI app `setup_dishka` attached the APP-scoped container to.
        # `request.state.dishka_container` is the REQUEST-scoped child dishka's
        # `ContainerMiddleware` already opened for this request — used as is, since
        # asking it for another REQUEST scope would ask dishka to enter a scope it is
        # already in. The `request.app.state` fallback still covers a panel mounted
        # without that middleware.
        container: AsyncContainer | None = getattr(request.state, "dishka_container", None)
        if container is not None:
            total, recent = await self._counts(container, since)
        else:
            async with request.app.state.dishka_container(scope=Scope.REQUEST) as owned:
                total, recent = await self._counts(owned, since)

        # `self.templates` is a `ClassVar[Jinja2Templates]` (`sqladmin/models.py`) that
        # `Admin.add_view`/`add_base_view` assigns onto the view class when it is
        # registered — `BaseView` itself carries no instance beyond the type
        # annotation. Using it (rather than building a second `Jinja2Templates`) is
        # what lets `dashboard.html` extend SQLAdmin's own `sqladmin/layout.html`; a
        # separate environment would not know that name. `TemplateResponse` is async
        # on this version of SQLAdmin, unlike Starlette's own.
        return await self.templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "total_users": total,
                "new_users": recent,
                "since": since,
                "window_days": NEW_USER_WINDOW_DAYS,
            },
        )

    @staticmethod
    async def _counts(container: AsyncContainer, since: datetime.datetime) -> tuple[int, int]:
        session = await container.get(AsyncSession)
        total = (await session.execute(select(func.count()).select_from(UserModel))).scalar_one()
        recent = (
            await session.execute(
                select(func.count()).select_from(UserModel).where(UserModel.created_at >= since),
            )
        ).scalar_one()
        return total, recent
