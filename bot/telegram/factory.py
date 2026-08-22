from __future__ import annotations
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

if TYPE_CHECKING:
    from bot.core.config import Settings


def create_bot(settings: Settings) -> Bot:
    """Build the Bot. Task 15 attaches the outbound rate limiter here."""
    return Bot(
        token=settings.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
