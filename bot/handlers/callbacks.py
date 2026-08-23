# ruff: noqa: TC001, TC002  - aiogram and dishka resolve this module's handler
# signatures at runtime via get_type_hints(), so these imports must stay at module level
import re

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.callback_answer import CallbackAnswer
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.core.config import Settings
from bot.keyboards.callback_data import MenuCB
from bot.keyboards.inline.contacts import support_keyboard
from bot.keyboards.inline.menu import main_keyboard

router = Router(name="callbacks")

_HTML_TAG_RE = re.compile(r"<[^>]+>")


async def _edit_or_answer(query: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup) -> None:
    """Render `text` on the tapped message, or fall back to a callback answer.

    `query.message` is `Message | InaccessibleMessage | None` in aiogram 3.30 - it is
    only editable when it is a real `Message` (not, e.g., a message too old for the
    Bot API to return full data for). Falling back to `answer()` still gives the user
    feedback instead of a silent no-op.

    The fallback goes through `query.answer()`, which renders as a plain-text toast
    with no parse mode - `text` may carry HTML (e.g. a `<b>...</b>` wrapper), so tags
    are stripped before it's shown there.
    """
    if isinstance(query.message, Message):
        await query.message.edit_text(text, reply_markup=reply_markup)
    else:
        await query.answer(_HTML_TAG_RE.sub("", text))


@router.callback_query(MenuCB.filter(F.action == "info"))
async def info_callback(query: CallbackQuery) -> None:
    await _edit_or_answer(query, _("about"), main_keyboard())


@router.callback_query(MenuCB.filter(F.action == "support"))
async def support_callback(query: CallbackQuery, settings: FromDishka[Settings]) -> None:
    await _edit_or_answer(query, _("support text"), support_keyboard(settings.bot.support_url))


@router.callback_query(MenuCB.filter(F.action == "back"))
async def back_callback(query: CallbackQuery) -> None:
    await _edit_or_answer(query, _("title main keyboard"), main_keyboard())


@router.callback_query(MenuCB.filter(F.action == "wallet"))
async def wallet_callback(_query: CallbackQuery, callback_answer: CallbackAnswer) -> None:
    # Placeholder: this template has no wallet domain. Answering explicitly is
    # better than a dead button - the user gets feedback instead of silence.
    #
    # `CallbackAnswerMiddleware` (registered in bot/middlewares/__init__.py) always
    # answers the query itself in its `finally` block unless we already have (tracked
    # via `callback_answer.answered`, which has no public setter). Calling
    # `query.answer(...)` directly here would leave that flag False, so the middleware
    # would send a second, blank `AnswerCallbackQuery` that can clobber this alert on
    # the client. Setting `text`/`show_alert` on the injected `CallbackAnswer` instead
    # lets the middleware perform the single answer, with our content.
    callback_answer.text = _("not available yet")
    callback_answer.show_alert = True
