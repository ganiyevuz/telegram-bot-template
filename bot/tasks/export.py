from __future__ import annotations

from aiogram import Bot
from aiogram.types import BufferedInputFile
from dishka import Scope
from loguru import logger

from bot.database.repositories import UserRepository
from bot.tasks import broker, get_container
from bot.utils.users_export import csv_filename, stream_users_csv


@broker.task(task_name="export:users")
async def export_users(chat_id: int) -> int:
    """Stream the users table into a CSV and deliver it."""
    container = get_container()
    bot = await container.get(Bot)

    async with container(scope=Scope.REQUEST) as request_container:
        users = await request_container.get(UserRepository)
        chunks = [chunk async for chunk in stream_users_csv(users.stream())]
        total = await users.count()

    await bot.send_document(
        chat_id=chat_id,
        document=BufferedInputFile(file=b"".join(chunks), filename=csv_filename()),
        caption=f"{total} users",
    )
    logger.info(f"export delivered | chat_id: {chat_id} | users: {total}")
    return total
