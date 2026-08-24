# ruff: noqa: TC001, TC002 - dishka resolves this constructor's annotations at runtime
from __future__ import annotations

from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import RedisError

from bot.cache.keys import CacheKeys
from bot.core.config import NotifierSettings
from bot.notifier.levels import AlertLevel

# The swallowed-count key outlives its cooldown marker deliberately: if a process dies
# between the marker expiring and the next alert consuming the count, the count is still
# there for whoever gets there next. A multiple of the cooldown is enough — this is a
# counter, not a ledger, and an orphan expiring is a lost "(xN suppressed)" note, not a
# lost alert.
_COUNT_TTL_FACTOR = 4


class NotifierService:
    """Fingerprinted alerting with a Redis cooldown.

    `send` never blocks on Telegram and never raises. It decides whether this alert is
    the first of its kind in the cooldown window and, if so, enqueues `notify:deliver`.
    Its callers are error handlers and health checks — the last places in a codebase
    that should acquire a new way to fail.
    """

    def __init__(self, redis: Redis, settings: NotifierSettings) -> None:
        self._redis = redis
        self._settings = settings

    async def send(self, level: AlertLevel, title: str, body: str, fingerprint: str) -> bool:
        """Enqueue an alert unless an identical one is already in its cooldown.

        Returns True when an alert was enqueued, False when it was suppressed or the
        notifier is unconfigured.
        """
        if self._settings.chat_id is None:
            # No Redis call at all on this path. An unconfigured notifier must cost
            # nothing, not merely return False after doing the work.
            return False

        cooldown = self._settings.cooldown_seconds
        marker = CacheKeys.notify(fingerprint)
        counter = CacheKeys.notify_count(fingerprint)

        try:
            first = await self._redis.set(marker, 1, nx=True, ex=cooldown)
            if not first:
                await self._redis.incr(counter)
                # Outlive the marker so the count survives to be reported.
                await self._redis.expire(counter, cooldown * _COUNT_TTL_FACTOR)
                return False
            # GETDEL, not GET-then-DEL: two replicas can clear different fingerprints
            # concurrently, and a non-atomic reset drops counts.
            raw = await self._redis.getdel(counter)
            suppressed = int(raw) if raw else 0
        except RedisError as exc:
            # Fail OPEN — enqueue anyway. This is the OPPOSITE direction to
            # bot/middlewares/dedup.py and throttling.py, and deliberately so: dropping a
            # throttle check degrades to unthrottled, but dropping an alert degrades to
            # blind. A Redis outage would otherwise silence alerting at exactly the moment
            # something is wrong. Duplicate alerts during an outage are an acceptable
            # price; silence is not.
            logger.warning(f"notifier cooldown unavailable, alerting anyway | error: {exc}")
            suppressed = 0

        # Imported lazily, not at module level. `bot.core.di` imports this service, and
        # `bot.tasks.notify` imports `bot.tasks`, whose `__init__` imports
        # `bot.core.di.create_container` — so a module-level import here closes the cycle
        # di -> notifier.service -> tasks.notify -> tasks -> di, and mypy reports it as
        # `Cannot determine type of "broker"`. Same reason and same shape as the lazy
        # imports in bot/middlewares/__init__.py.
        from bot.tasks.notify import deliver_alert  # noqa: PLC0415

        await deliver_alert.kiq(str(level), title, body, suppressed)
        return True
