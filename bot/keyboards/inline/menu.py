from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.callback_data import MenuCB


def main_keyboard(webapp_url: str | None = None) -> InlineKeyboardMarkup:
    """Use in main menu.

    `webapp_url` is `WEBAPP_URL` (`settings.webapp.url`), passed in by the handler —
    this module never reads settings itself. When it is unset the Mini App button is
    left out of the markup entirely rather than built with an empty URL: Telegram
    rejects `web_app` buttons whose URL is blank and drops the *whole* keyboard in
    response, so the four normal buttons would disappear along with it.
    """
    buttons = [
        [InlineKeyboardButton(text=_("wallet button"), callback_data=MenuCB(action="wallet").pack())],
        [InlineKeyboardButton(text=_("premium button"), callback_data=MenuCB(action="premium").pack())],
        [InlineKeyboardButton(text=_("info button"), callback_data=MenuCB(action="info").pack())],
        [InlineKeyboardButton(text=_("support button"), callback_data=MenuCB(action="support").pack())],
    ]
    layout: tuple[int, ...] = (1, 1, 2)

    if webapp_url:
        buttons.insert(0, [InlineKeyboardButton(text=_("mini app button"), web_app=WebAppInfo(url=webapp_url))])
        layout = (1, 1, 1, 2)

    keyboard = InlineKeyboardBuilder(markup=buttons)

    keyboard.adjust(*layout)

    return keyboard.as_markup()
