from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message, User
from loguru import logger

from bot.analytics.types import BaseEvent, EventProperties, UserProperties

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

    from bot.analytics.types import AbstractAnalyticsLogger


class AnalyticsMiddleware(BaseMiddleware):
    """Tracks handlers marked with `@flags.analytics_event("Name")`.

    The handler runs first and its result is returned regardless of whether
    tracking succeeds, so the analytics provider can never break the bot.
    """

    def __init__(self, gateway: AbstractAnalyticsLogger) -> None:
        self._gateway = gateway
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        event_name = get_flag(data, "analytics_event")
        result = await handler(event, data)
        if event_name and isinstance(event, Message | CallbackQuery) and (user := event.from_user):
            try:
                await self._gateway.log_event(self._build(event, user, event_name))
            except Exception as exc:  # noqa: BLE001 - analytics must never break a handler
                logger.warning(f"analytics tracking failed | event: {event_name} | error: {exc}")
        return result

    def _build(self, event: Message | CallbackQuery, user: User, event_name: str) -> BaseEvent:
        chat_id: int | None
        chat_type: str | None
        if isinstance(event, Message):
            chat_id, chat_type, text = event.chat.id, event.chat.type, event.text
            command = event.text if event.text and event.text.startswith("/") else None
        else:
            message = event.message
            chat_id = message.chat.id if message else None
            chat_type = message.chat.type if message else None
            text, command = event.data, None
        return BaseEvent(
            user_id=user.id,
            event_type=event_name,
            user_properties=UserProperties(
                first_name=user.first_name,
                last_name=user.last_name,
                username=user.username,
                url=user.url,
            ),
            event_properties=EventProperties(
                chat_id=chat_id,
                chat_type=chat_type,
                text=text,
                command=command,
            ),
            language=user.language_code,
        )
