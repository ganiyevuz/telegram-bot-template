# ruff: noqa: TC002  - these annotations are resolved at runtime (aiogram inspects handler and filter signatures), so the imports must stay at module level
from aiogram.filters import BaseFilter
from aiogram.types import Message


class NumberFilter(BaseFilter):
    """Allows only numbers with or without a dot."""

    async def __call__(self, message: Message) -> bool:
        if not message.text:
            return False

        try:
            float(message.text)
        except ValueError:
            return False
        else:
            return True
