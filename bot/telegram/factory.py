from __future__ import annotations
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.telegram.ratelimit import OutboundRateLimiter

if TYPE_CHECKING:
    from bot.cache.ratelimit import TokenBucket
    from bot.core.config import Settings


def create_bot(settings: Settings, global_bucket: TokenBucket, chat_bucket: TokenBucket) -> Bot:
    bot = Bot(
        token=settings.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware.register(OutboundRateLimiter(global_bucket, chat_bucket))
    return bot
