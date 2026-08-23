# ruff: noqa: RUF012, TC002 - SQLAdmin reads these class attributes as plain
# lists, and resolves the view signatures at runtime
from __future__ import annotations
from typing import Any

from sqladmin import ModelView
from sqladmin.filters import BooleanFilter, OperationColumnFilter
from starlette.requests import Request
from wtforms.fields import PasswordField
from wtforms.form import Form
from wtforms.validators import InputRequired

from bot.admin.auth import SUPERUSER_SESSION_KEY
from bot.admin.security import hash_password
from bot.database.models import AdminModel, RoleModel, UserModel


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
