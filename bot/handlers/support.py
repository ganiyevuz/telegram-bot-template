# ruff: noqa: TC001, TC002  - dishka resolves handler annotations at runtime via get_type_hints, so every
# annotation on an injected handler must be importable at module level
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.core.config import Settings
from bot.keyboards.inline.contacts import contacts_keyboard

router = Router(name="support")


@router.message(Command(commands=["supports", "support", "contacts", "contact"]))
async def support_handler(message: Message, settings: FromDishka[Settings]) -> None:
    """Return a button with a link to the project."""
    await message.answer(_("support text"), reply_markup=contacts_keyboard(settings.bot.support_url))
