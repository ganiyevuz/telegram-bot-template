from __future__ import annotations
import csv
import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bot.database.models import UserModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator

COLUMNS = [column.name for column in UserModel.__table__.columns]


def csv_filename() -> str:
    return f"users_{datetime.now(UTC).strftime('%Y.%m.%d_%H.%M')}.csv"


async def stream_users_csv(users: AsyncIterable[UserModel], chunk_rows: int = 500) -> AsyncIterator[bytes]:
    """Yield CSV bytes in chunks, never holding the full table in memory."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    rows = 0

    async for user in users:
        writer.writerow([getattr(user, column) for column in COLUMNS])
        rows += 1
        if rows % chunk_rows == 0:
            yield buffer.getvalue().encode()
            buffer.seek(0)
            buffer.truncate(0)

    tail = buffer.getvalue()
    if tail:
        yield tail.encode()
