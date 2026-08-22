from __future__ import annotations
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update

from bot.database.models import UserModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiogram.types import User as TgUser
    from sqlalchemy.ext.asyncio import AsyncSession


class UserRepository:
    """All access to the `users` table. No caching here — that is the service layer."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> UserModel | None:
        return await self._session.get(UserModel, user_id)

    async def exists(self, user_id: int) -> bool:
        query = select(UserModel.id).filter_by(id=user_id).limit(1)
        return (await self._session.execute(query)).scalar_one_or_none() is not None

    async def create(self, user: TgUser, referrer: str | None) -> UserModel:
        new_user = UserModel(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            language_code=user.language_code,
            is_premium=user.is_premium or False,
            referrer=referrer,
        )
        self._session.add(new_user)
        await self._session.commit()
        return new_user

    async def language_of(self, user_id: int) -> str | None:
        query = select(UserModel.language_code).filter_by(id=user_id)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def first_name_of(self, user_id: int) -> str | None:
        query = select(UserModel.first_name).filter_by(id=user_id)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def is_admin(self, user_id: int) -> bool:
        query = select(UserModel.is_admin).filter_by(id=user_id)
        return bool((await self._session.execute(query)).scalar_one_or_none())

    async def set_language(self, user_id: int, language_code: str) -> None:
        await self._update(user_id, language_code=language_code)

    async def set_admin(self, user_id: int, *, value: bool) -> None:
        await self._update(user_id, is_admin=value)

    async def set_blocked(self, user_id: int, *, value: bool) -> None:
        await self._update(user_id, is_block=value)

    async def _update(self, user_id: int, **values: object) -> None:
        await self._session.execute(update(UserModel).where(UserModel.id == user_id).values(**values))
        await self._session.commit()

    async def count(self) -> int:
        query = select(func.count()).select_from(UserModel)
        return int((await self._session.execute(query)).scalar_one_or_none() or 0)

    async def stream(self, batch_size: int = 1000) -> AsyncIterator[UserModel]:
        """Yield every user using keyset pagination.

        Replaces `get_all_users()`, which selected the whole table into a list.
        Memory stays bounded by `batch_size` regardless of table size, and keyset
        paging does not degrade on deep pages the way OFFSET does.
        """
        last_id = 0
        while True:
            query = select(UserModel).where(UserModel.id > last_id).order_by(UserModel.id).limit(batch_size)
            rows = (await self._session.execute(query)).scalars().all()
            if not rows:
                return
            for row in rows:
                yield row
            last_id = rows[-1].id
