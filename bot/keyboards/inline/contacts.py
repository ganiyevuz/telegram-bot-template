from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.callback_data import MenuCB


def contacts_keyboard(support_url: str | None) -> InlineKeyboardMarkup:
    """Use when call contacts command."""
    buttons = [
        [InlineKeyboardButton(text=_("support button"), url=support_url)],
    ]

    keyboard = InlineKeyboardBuilder(markup=buttons)

    return keyboard.as_markup()


def support_keyboard(support_url: str | None) -> InlineKeyboardMarkup:
    """Use when call support query."""
    buttons = [
        [InlineKeyboardButton(text=_("support button"), url=support_url)],
        [InlineKeyboardButton(text=_("back button"), callback_data=MenuCB(action="back").pack())],
    ]

    keyboard = InlineKeyboardBuilder(markup=buttons)

    return keyboard.as_markup()
