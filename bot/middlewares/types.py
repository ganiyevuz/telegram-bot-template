from __future__ import annotations
from typing import TYPE_CHECKING

from aiogram.dispatcher.middlewares.data import MiddlewareData as AiogramMiddlewareData

if TYPE_CHECKING:
    from bot.services.users import UserService


class MiddlewareData(AiogramMiddlewareData, total=False):
    """Context keys this project adds on top of aiogram's own."""

    user_service: UserService
