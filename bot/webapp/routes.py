# ruff: noqa: TC001, TC002  - FastAPI and dishka resolve these route signatures at
# runtime via get_type_hints(); these imports must stay at module level
from aiogram import Bot
from aiogram.utils.i18n.core import I18n
from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter
from pydantic import BaseModel

from bot.core.config import DEFAULT_LOCALE
from bot.services.payments import PaymentService
from bot.services.users import UserService
from bot.webapp.dependencies import CurrentWebAppUser

router = APIRouter(prefix="/api/webapp", tags=["webapp"], route_class=DishkaRoute)


class MeResponse(BaseModel):
    id: int
    first_name: str
    language_code: str
    is_premium: bool


class InvoiceResponse(BaseModel):
    invoice_link: str


async def _locale_of(users: UserService, user_id: int) -> str:
    """The caller's stored locale, or the default when they have no row yet."""
    return await users.language_of(user_id) or DEFAULT_LOCALE


@router.get("/me")
async def me(user: CurrentWebAppUser, users: FromDishka[UserService]) -> MeResponse:
    """The caller's own profile — never anybody else's.

    The stored first name wins over the one inside `initData` because `/settings` can
    change ours, but falls back to it for a user who has opened the Mini App without
    ever having messaged the bot (no row exists yet, so the lookups return empty).
    """
    return MeResponse(
        id=user.id,
        first_name=await users.first_name_of(user.id) or user.first_name,
        language_code=await _locale_of(users, user.id),
        # Our own paid premium, from the database — deliberately not `user.is_premium`,
        # which is the caller's *Telegram* Premium status and says nothing about
        # whether they have paid us.
        is_premium=await users.is_premium(user.id),
    )


@router.post("/invoice")
async def invoice(
    user: CurrentWebAppUser,
    bot: FromDishka[Bot],
    payments: FromDishka[PaymentService],
    users: FromDishka[UserService],
    i18n: FromDishka[I18n],
) -> InvoiceResponse:
    """A subscription invoice link for the caller, to open with `tg.openInvoice(...)`."""
    locale = await _locale_of(users, user.id)
    # aiogram's `gettext` reads the current I18n and locale from ContextVars that only
    # its own middleware sets, so the `_()` calls inside `subscription_link` raise
    # `LookupError: I18n context is not set` when the caller is a FastAPI route instead
    # of an update handler. `context()` makes this instance the current one and
    # `use_locale()` pins the locale — the same pattern `set_default_commands` uses for
    # its own contextless startup call. Awaiting inside the block keeps the context:
    # coroutines run in the caller's context, only Tasks copy it.
    with i18n.context(), i18n.use_locale(locale):
        link = await payments.subscription_link(bot, user.id)
    return InvoiceResponse(invoice_link=link)
