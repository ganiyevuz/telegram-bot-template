# ruff: noqa: TC001, TC002  - runtime-resolved handler signature
from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.filters.admin import AdminFilter
from bot.services.payments import PaymentService

router = Router(name="admin_payments")

REFUND_ARGS_COUNT = 2  # USER_ID CHARGE_ID


@router.message(Command(commands="refund"), AdminFilter())
async def refund_command(
    message: Message,
    command: CommandObject,
    bot: FromDishka[Bot],
    payments: FromDishka[PaymentService],
) -> None:
    """/refund <user_id> <charge_id>"""
    parts = (command.args or "").split()
    if len(parts) != REFUND_ARGS_COUNT or not parts[0].isdigit():
        # No angle brackets: the bot's default parse mode is HTML (see
        # bot/telegram/factory.py), so "<user_id>" would be read as an unknown HTML
        # tag and Telegram would reject the whole message with "can't parse entities".
        await message.answer(_("usage: /refund USER_ID CHARGE_ID"))
        return

    ok = await payments.refund(bot, int(parts[0]), parts[1])
    await message.answer(_("refunded") if ok else _("unknown charge id"))
