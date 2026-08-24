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
HANDLER_ERRORS = Counter(
    f"{PREFIX}_handler_errors_total",
    "Exceptions reaching the global error handler, including pre-handler middleware failures.",
    ["exception"],
)
# Deliberately separate from HANDLER_ERRORS, which — despite its name — is also
# where a dispatch-chain failure (e.g. FSMContextMiddleware hitting Redis) actually
# gets counted today: aiogram's own `ErrorsMiddleware` wraps the ENTIRE outer-
# middleware chain and routes any exception through bot/handlers/errors.py's
# `on_error`, which always returns `True` and is what increments HANDLER_ERRORS —
# so in the common case that exception never reaches bot/api/webhook.py's except
# block at all. This counter is the backstop for the exceptions that DO reach that
# block regardless (see its comment for exactly when that is) — conflating the two
# would hide which boundary actually caught a given failure, and HANDLER_ERRORS
# already carries the everyday Redis-outage signal.
WEBHOOK_DISPATCH_FAILURES = Counter(
    f"{PREFIX}_webhook_dispatch_failures_total",
    "Updates dropped by bot/api/webhook.py's own guard, after aiogram's error boundary did not catch them.",
    ["exception"],
)
OUTBOUND_WAITS = Counter(f"{PREFIX}_outbound_rate_limit_waits_total", "Outbound calls delayed.", ["scope"])
OUTBOUND_RETRY_AFTER = Counter(f"{PREFIX}_outbound_retry_after_total", "429s returned by Telegram.")
# Labelled by a closed set of reasons (`missing`, `invalid`, `expired`) — never the
# rejected header or any part of it, which is attacker-controlled and would give this
# counter unbounded cardinality. A spike here is a forgery attempt or a client whose
# `initData` has gone stale; the two are only distinguishable by the label, since the
# 401 body deliberately looks identical to the caller.
WEBAPP_INIT_DATA_REJECTIONS = Counter(
    f"{PREFIX}_webapp_init_data_rejections_total",
    "Mini App requests rejected before reaching a route.",
    ["reason"],
)
# Same closed-label discipline as WEBAPP_INIT_DATA_REJECTIONS above, and for the same
# reason: `disabled` (no NOTIFIER_SECRET), `stale` (X-Timestamp outside the window),
# `signature` (HMAC did not verify), `payload` (body did not match the schema). Never the
# caller-supplied `source` — that is attacker-controlled on a public endpoint and would
# give this counter unbounded cardinality. Every rejection looks identical to the caller,
# so this label is the only place the reason is visible.
NOTIFY_REJECTIONS = Counter(
    f"{PREFIX}_notify_rejections_total",
    "Inbound /api/notify requests rejected before an alert was enqueued.",
    ["reason"],
)


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
