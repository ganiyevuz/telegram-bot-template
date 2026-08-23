from __future__ import annotations
import time
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import Update
from prometheus_client import Counter, Histogram

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

PREFIX = "tgbot"

UPDATES = Counter(f"{PREFIX}_updates_total", "Updates processed.", ["event_type", "outcome"])
UPDATE_DURATION = Histogram(f"{PREFIX}_update_duration_seconds", "Update processing time.", ["event_type"])
THROTTLED = Counter(f"{PREFIX}_throttled_total", "Updates dropped by throttling.", ["scope"])
DEDUP_HITS = Counter(f"{PREFIX}_dedup_hits_total", "Updates dropped as duplicates.")
HANDLER_ERRORS = Counter(f"{PREFIX}_handler_errors_total", "Unhandled handler exceptions.", ["exception"])


def _event_type(event: TelegramObject) -> str:
    if isinstance(event, Update):
        return event.event_type
    return type(event).__name__


class MetricsMiddleware(BaseMiddleware):
    """RED metrics per update. Registered on `dp.update`, so it works in polling
    mode too — the old aiohttp-only middleware left polling deployments blind."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        event_type = _event_type(event)
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await handler(event, data)
        except Exception:
            outcome = "error"
            raise
        finally:
            UPDATE_DURATION.labels(event_type=event_type).observe(time.perf_counter() - started)
            UPDATES.labels(event_type=event_type, outcome=outcome).inc()
