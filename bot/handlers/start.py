# ruff: noqa: TC001, TC002  - dishka resolves handler annotations at runtime via get_type_hints, so every
# annotation on an injected handler must be importable at module level
from aiogram import Router, flags, types
from aiogram.filters import CommandStart
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.core.config import Settings
from bot.keyboards.inline.menu import main_keyboard

router = Router(name="start")


@router.message(CommandStart())
@flags.analytics_event("Sign Up")
async def start_handler(message: types.Message, settings: FromDishka[Settings]) -> None:
    """Welcome message."""
    await message.answer(_("first message"), reply_markup=main_keyboard(settings.webapp.url))
