# ruff: noqa: TC001, TC002  - dishka resolves handler annotations at runtime via get_type_hints, so every
# annotation on an injected handler must be importable at module level
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.database.repositories import UserRepository
from bot.filters.admin import AdminFilter
from bot.services.users import UserService
from bot.utils.users_export import csv_filename, stream_users_csv

router = Router(name="export_users")


@router.message(Command(commands="export_users"), AdminFilter())
async def export_users_handler(
    message: Message,
    users: FromDishka[UserRepository],
    user_service: FromDishka[UserService],
) -> None:
    """Export all users as CSV. Task 14 moves this to a background task."""
    chunks = [chunk async for chunk in stream_users_csv(users.stream())]
    document = BufferedInputFile(file=b"".join(chunks), filename=csv_filename())
    count = await user_service.count()
    await message.answer_document(document=document, caption=_("user counter: <b>{count}</b>").format(count=count))
