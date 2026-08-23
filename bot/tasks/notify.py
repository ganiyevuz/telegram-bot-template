from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from loguru import logger

from bot.core.config import Settings
from bot.notifier import AlertLevel
from bot.tasks import broker, get_container


@broker.task(task_name="notify:deliver")
async def deliver_alert(level: str, title: str, body: str, suppressed: int = 0) -> bool:
    """Send one alert. Returns False when the notifier is not configured.

    Never raises: this runs in a worker, and a raising task is retried or dead-lettered,
    turning one failed alert into a loop — usually while the thing being alerted about
    is already an outage.
    """
    container = get_container()
    settings = await container.get(Settings)

    chat_id = settings.notifier.chat_id
    if chat_id is None:
        # Unconfigured notifier is a choice, not a fault: warning on every alert would
        # make the feature its own noise source.
        logger.debug(f"notifier not configured | level: {level} | title: {title}")
        return False

    bot = await container.get(Bot)
    text = AlertLevel(level).format(title, body, suppressed)
    topic_id = settings.notifier.topic_id

    try:
        if topic_id is not None:
            await bot.send_message(chat_id=chat_id, text=text, message_thread_id=topic_id)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
    except TelegramAPIError as exc:
        logger.warning(f"alert delivery failed | level: {level} | title: {title} | error: {exc}")
        return False
    return True
