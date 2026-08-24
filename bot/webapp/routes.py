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
from bot.webapp.initdata import WebAppUser

router = APIRouter(prefix="/api/webapp", tags=["webapp"], route_class=DishkaRoute)


class MeResponse(BaseModel):
    id: int
    first_name: str
    language_code: str
    is_premium: bool


class InvoiceResponse(BaseModel):
    invoice_link: str


async def _locale_of(users: UserService, i18n: I18n, user: WebAppUser) -> str:
    """The locale to answer this caller in.

    Stored value first — `/settings` is an explicit choice and must win. Then the
    `language_code` from the *verified* `initData`, which is the only signal we have for
    a caller who has never messaged the bot: a `t.me/<bot>?startapp=...` deep link opens
    the Mini App without creating a row, so falling straight through to English would
    hand a Russian speaker an English page on the one interaction most likely to decide
    whether they stay. It is the client's own Telegram language, so it can be anything
    (`de`, `pt-br`, ...); accepted only on exact membership of what the bot actually
    ships, read off `I18n` so adding a locale later does not silently skip this path.
    """
    stored = await users.language_of(user.id)
    if stored:
        return stored
    claimed = user.language_code
    if claimed and claimed in i18n.available_locales:
        return claimed
    return DEFAULT_LOCALE


@router.get("/me")
async def me(user: CurrentWebAppUser, users: FromDishka[UserService], i18n: FromDishka[I18n]) -> MeResponse:
    """The caller's own profile — never anybody else's.

    The stored first name wins over the one inside `initData` because `/settings` can
    change ours, but falls back to it for a user who has opened the Mini App without
    ever having messaged the bot (no row exists yet, so the lookups return empty).
    """
    return MeResponse(
        id=user.id,
        first_name=await users.first_name_of(user.id) or user.first_name,
        language_code=await _locale_of(users, i18n, user),
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
    locale = await _locale_of(users, i18n, user)
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
