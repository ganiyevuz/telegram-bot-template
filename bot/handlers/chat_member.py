# ruff: noqa: TC001, TC002  - aiogram and dishka resolve this handler's signature at
# runtime via get_type_hints(), so these imports must stay at module level
from aiogram import Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberUpdated
from dishka.integrations.aiogram import FromDishka
from loguru import logger

from bot.services.users import UserService

router = Router(name="chat_member")

_BLOCKED = {ChatMemberStatus.KICKED, ChatMemberStatus.LEFT}


@router.my_chat_member()
async def track_block_state(event: ChatMemberUpdated, users: FromDishka[UserService]) -> None:
    """Keep `is_block` accurate the moment a user blocks or unblocks the bot.

    Telegram sends this as soon as the state changes, so we no longer have to
    discover blocked users by failing to send to them during a broadcast.
    """
    blocked = event.new_chat_member.status in _BLOCKED
    was_blocked = event.old_chat_member.status in _BLOCKED
    if blocked == was_blocked:
        return

    await users.mark_blocked(event.from_user.id, value=blocked)
    logger.info(
        f"chat member state changed | user_id: {event.from_user.id} | blocked: {blocked}",
    )
