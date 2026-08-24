# ruff: noqa: TC002  - these annotations are resolved at runtime (aiogram inspects handler and filter signatures), so the imports must stay at module level
from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram.filters import BaseFilter
from aiogram.types import Message

from bot.services.users import UserService

if TYPE_CHECKING:
    from dishka import AsyncContainer


class AdminFilter(BaseFilter):
    """Allows only administrators (whose database column is_admin=True)."""

    async def __call__(self, message: Message, **data: Any) -> bool:
        if not message.from_user:
            return False
        container: AsyncContainer = data["dishka_container"]
        users = await container.get(UserService)
        return await users.is_admin(message.from_user.id)
