# ruff: noqa: RUF012, TC002 - SQLAdmin reads these class attributes as plain
# lists, and resolves the view signatures at runtime
from __future__ import annotations
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from dishka import AsyncContainer, Scope
from loguru import logger
from sqladmin import Flash, ModelView, action
from sqladmin.filters import BooleanFilter, OperationColumnFilter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.status import HTTP_302_FOUND
from wtforms.fields import PasswordField
from wtforms.form import Form
from wtforms.validators import InputRequired

from bot.admin.auth import SUPERUSER_SESSION_KEY
from bot.admin.security import hash_password
from bot.database.models import AdminModel, PaymentModel, PaymentStatus, RoleModel, UserModel
from bot.services.payments import PaymentService


class SuperuserOnly:
    """Restrict a view to admins holding the `superuser` role.

    `is_visible` as well as `is_accessible`: with only the latter the menu entry
    renders for everyone and 403s on click, which reads as a broken panel rather than
    a permission boundary — and with only the former the view is hidden but still
    reachable by typing the URL, which is the actual vulnerability.
    """

    def _is_superuser(self, request: Request) -> bool:
        # `AdminAuth.authenticate` re-reads the role from the database on every
        # request and rewrites this key, so revoking `superuser` closes these views on
        # the next request rather than at session expiry.
        return bool(request.session.get(SUPERUSER_SESSION_KEY))

    def is_accessible(self, request: Request) -> bool:
        return self._is_superuser(request)

    def is_visible(self, request: Request) -> bool:
        return self._is_superuser(request)


class UserAdmin(ModelView, model=UserModel):
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-users"

    # Mirrors the Flask panel's column_list exactly — see the parity table in the plan.
    column_list = [
        UserModel.id,
        UserModel.username,
        UserModel.first_name,
        UserModel.last_name,
        UserModel.language_code,
        UserModel.is_admin,
        UserModel.is_suspicious,
        UserModel.is_block,
        UserModel.is_premium,
        UserModel.created_at,
    ]
    column_searchable_list = [UserModel.id, UserModel.username, UserModel.first_name, UserModel.last_name]
    column_sortable_list = column_list
    column_default_sort = ("created_at", True)
    # Same five filters the Flask panel offered. SQLAdmin wants filter objects rather
    # than bare columns: a column in this list has no `title` and blows up the list
    # template.
    column_filters = [
        BooleanFilter(UserModel.is_admin),
        BooleanFilter(UserModel.is_suspicious),
        BooleanFilter(UserModel.is_block),
        BooleanFilter(UserModel.is_premium),
        OperationColumnFilter(UserModel.created_at),
    ]

    # No create: a Telegram user row is written by AuthMiddleware when someone messages
    # the bot. Hand-creating one would produce a row with an id that belongs to nobody.
    can_create = False
    can_edit = True
    can_delete = True
    can_view_details = True
    can_export = True


class RoleAdmin(SuperuserOnly, ModelView, model=RoleModel):
    name = "Role"
    name_plural = "Roles"
    icon = "fa-solid fa-tags"

    column_list = [RoleModel.id, RoleModel.name, RoleModel.description]
    column_sortable_list = column_list
    # Read-only, as in the Flask panel: the two roles are seeded by the migration and
    # the code checks them by name, so a renamed role silently removes access.
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    can_export = False


class AdminAdmin(SuperuserOnly, ModelView, model=AdminModel):
    name = "Admin"
    name_plural = "Admins"
    icon = "fa-solid fa-user-shield"

    column_list = [AdminModel.id, AdminModel.email, AdminModel.first_name, AdminModel.last_name, AdminModel.active]
    column_sortable_list = column_list
    column_searchable_list = [AdminModel.email, AdminModel.first_name, AdminModel.last_name]
    column_filters = [
        OperationColumnFilter(AdminModel.email),
        OperationColumnFilter(AdminModel.first_name),
        OperationColumnFilter(AdminModel.last_name),
    ]
    # `password` appears in NO list: not in column_list, not in details. It holds a
    # digest rather than a password, but rendering it hands an attacker with read
    # access something to attack offline.
    column_details_exclude_list = [AdminModel.password]
    # `roles` is in the form because the Flask panel had it there: it is the only way
    # to grant `superuser`, and without it the seeded admin is the last superuser this
    # panel can ever have.
    form_columns = [
        AdminModel.email,
        AdminModel.first_name,
        AdminModel.last_name,
        AdminModel.active,
        AdminModel.roles,
        AdminModel.password,
    ]
    # A PasswordField renders empty rather than echoing the stored digest back into
    # the edit form's HTML.
    form_overrides = {"password": PasswordField}
    form_widget_args = {"password": {"autocomplete": "new-password"}}
    form_args = {"password": {"description": "Leave blank to keep the current password."}}

    can_create = True
    can_edit = True
    can_delete = True
    can_view_details = True
    can_export = False

    async def scaffold_form(self, rules: list[str] | None = None) -> type[Form]:
        """Drop the `InputRequired` SQLAdmin adds to the non-nullable password column.

        Without this an edit that leaves the password blank is rejected by form
        validation, so the "blank means keep the current password" branch below is
        unreachable and editing an admin means retyping their password. Creating one
        without a password is refused in `on_model_change` instead.
        """
        form = await super().scaffold_form(rules)
        password = form.password
        password.kwargs["validators"] = [
            validator for validator in password.kwargs.get("validators", ()) if not isinstance(validator, InputRequired)
        ]
        return form

    async def on_model_change(self, data: dict[str, Any], model: Any, is_created: bool, request: Request) -> None:  # noqa: ARG002
        """Hash whatever was typed into the password field before it is stored.

        Without this the form value is written verbatim and every admin password is
        stored in plaintext. An empty field on edit means "leave it alone" — otherwise
        editing an admin's name would blank their password and lock them out.
        """
        password = str(data.get("password") or "")
        if password:
            data["password"] = hash_password(password)
        elif is_created:
            # Reachable because `scaffold_form` made the field optional. Storing the
            # empty string would create an admin nobody can ever log in as.
            msg = "A password is required when creating an admin."
            raise ValueError(msg)
        else:
            data.pop("password", None)


# Shown in the confirmation modal before the action runs. The list page fires this from
# a row checkbox, so without a confirmation a misclick moves real money — and it cannot
# be walked back: Telegram rejects a second `refund_star_payment` for the same charge.
REFUND_CONFIRMATION = "Refund the selected Stars payments? This calls Telegram and cannot be undone."


def _selected_ids(request: Request) -> list[int]:
    """The primary keys SQLAdmin puts in `?pks=` — the rows ticked on the list page.

    Non-numeric entries are dropped rather than raising: the query string is whatever
    was typed into the URL bar, and a `ValueError` here would replace the list with
    SQLAdmin's error page instead of telling the operator nothing was selected.
    """
    raw = request.query_params.get("pks", "")
    return [int(pk) for pk in raw.split(",") if pk.isdigit()]


def _flash_outcome(
    request: Request,
    *,
    refunded: list[int],
    already: list[int],
    skipped: list[int],
    failed: list[str],
) -> None:
    """Report every bucket the action produced, including the ones it did nothing for.

    A refund that was skipped looks exactly like one that succeeded if the only
    feedback is a redirect back to the list, and the operator would reasonably click
    again.
    """
    if refunded:
        Flash.success(request, f"Refunded {len(refunded)} payment(s): {', '.join(f'#{pk}' for pk in refunded)}.")
    if already:
        Flash.warning(request, f"Already refunded, left untouched: {', '.join(f'#{pk}' for pk in already)}.")
    if skipped:
        Flash.error(request, f"No payment recorded for: {', '.join(f'#{pk}' for pk in skipped)}.")
    if failed:
        Flash.error(request, f"Telegram rejected the refund for: {'; '.join(failed)}.")


class PaymentAdmin(ModelView, model=PaymentModel):
    name = "Payment"
    name_plural = "Payments"
    icon = "fa-solid fa-star"

    column_list = [
        PaymentModel.id,
        PaymentModel.user_id,
        PaymentModel.amount,
        PaymentModel.currency,
        PaymentModel.status,
        PaymentModel.is_recurring,
        PaymentModel.subscription_expires_at,
        PaymentModel.created_at,
    ]
    column_searchable_list = [PaymentModel.user_id, PaymentModel.telegram_payment_charge_id]
    column_default_sort = ("created_at", True)

    # A payment row is a record of something that happened. Editing or deleting one
    # makes the local ledger disagree with Telegram's, which is the exact drift
    # `payments:reconcile` exists to detect.
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True
    can_export = True

    def _back_to_list(self, request: Request) -> RedirectResponse:
        return RedirectResponse(request.url_for("admin:list", identity=self.identity), status_code=HTTP_302_FOUND)

    @action(name="refund", label="Refund selected payments", confirmation_message=REFUND_CONFIRMATION)
    async def refund_payments(self, request: Request) -> RedirectResponse:
        """Refund the ticked charges through `PaymentService.refund`, then go back to the list.

        Every part of the decision belongs to the service: whether the charge is one of
        ours, whether it belongs to that user, and — the part worth naming — whether to
        revoke premium at all, which is only correct when no other unrefunded paid
        period is still running. A user can hold two concurrent subscriptions, so
        clearing `is_premium` on any refund revokes a period they paid for and still
        hold, permanently. That bug has been fixed once in the service already;
        re-deriving it here would put it straight back.
        """
        ids = _selected_ids(request)
        if not ids:
            Flash.warning(request, "Select at least one payment to refund.")
            return self._back_to_list(request)

        # SQLAdmin actions run outside dishka's FastAPI integration, so `FromDishka` is
        # unavailable and the container has to come off the request. `request.state`
        # first is not a fallback ordering, it is the working one: SQLAdmin mounts its
        # own Starlette sub-application, and Starlette's `__call__` sets
        # `scope["app"] = self`, so inside an action `request.app` is that sub-app — NOT
        # the FastAPI app `setup_dishka` attached the APP-scoped container to. What does
        # survive the mount is the ASGI scope's `state`, where dishka's
        # `ContainerMiddleware` leaves the REQUEST-scoped child it opened for this
        # request. That one is used as it is: asking it for another REQUEST scope would
        # ask dishka to enter a scope it is already in. `request.app.state` still covers
        # a panel mounted without that middleware.
        container: AsyncContainer | None = getattr(request.state, "dishka_container", None)
        if container is not None:
            await self._refund_selected(request, container, ids)
        else:
            async with request.app.state.dishka_container(scope=Scope.REQUEST) as owned:
                await self._refund_selected(request, owned, ids)
        return self._back_to_list(request)

    async def _refund_selected(self, request: Request, container: AsyncContainer, ids: list[int]) -> None:
        bot = await container.get(Bot)
        payments = await container.get(PaymentService)
        session = await container.get(AsyncSession)

        query = select(PaymentModel).where(PaymentModel.id.in_(ids)).order_by(PaymentModel.id)
        rows = list(await session.scalars(query))

        refunded: list[int] = []
        already: list[int] = []
        refused: list[int] = []
        failed: list[str] = []
        for payment in rows:
            if payment.status == PaymentStatus.REFUNDED:
                # Telegram rejects a second `refund_star_payment` for the same charge, so
                # a double-click has to stop here rather than turning into an opaque
                # "Bad Request" the operator cannot interpret.
                already.append(payment.id)
                continue
            try:
                ok = await payments.refund(bot, payment.user_id, payment.telegram_payment_charge_id)
            except TelegramAPIError as exc:
                # One charge Telegram refuses must not abandon the rest of the selection
                # half-done, and the operator needs the reason rather than a 500 page.
                logger.warning(f"admin refund rejected | payment: {payment.id} | error: {exc}")
                failed.append(f"#{payment.id} ({exc.message})")
                continue
            # False means the service does not recognise the charge as this user's, which
            # a row loaded from this same table should never be — report it rather than
            # counting it as refunded.
            (refunded if ok else refused).append(payment.id)

        # `refused` and the ids that matched no row are one bucket to the operator: this
        # panel holds no payment it can refund under that number.
        skipped = sorted(refused + list(set(ids) - {payment.id for payment in rows}))
        _flash_outcome(request, refunded=refunded, already=already, skipped=skipped, failed=failed)
        logger.info(
            f"admin refund action | refunded: {refunded} | already: {already} | "
            f"skipped: {skipped} | failed: {len(failed)}",
        )
