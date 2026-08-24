# ruff: noqa: TC001, TC002  - dishka resolves handler annotations at runtime via get_type_hints, so every
# annotation on an injected handler must be importable at module level
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.core.config import Settings
from bot.keyboards.inline.menu import main_keyboard

router = Router(name="menu")


@router.message(Command(commands=["menu", "main"]))
async def menu_handler(message: types.Message, settings: FromDishka[Settings]) -> None:
    """Return main menu."""
    await message.answer(_("title main keyboard"), reply_markup=main_keyboard(settings.webapp.url))
