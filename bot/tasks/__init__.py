from __future__ import annotations
from typing import TYPE_CHECKING

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from bot.core.config import get_settings
from bot.core.di import create_container
from bot.core.logging import setup_logging

if TYPE_CHECKING:
    from dishka import AsyncContainer

_settings = get_settings()

# `socket_timeout=None`: this broker's `listen()` issues an unbounded BRPOP (no
# server-side timeout) and only catches `ConnectionError`, not `TimeoutError` —
# with redis-py's default 5s client-side `socket_timeout`, an idle queue makes
# the *local* read time out every 5s, and that `redis.exceptions.TimeoutError`
# is not a `ConnectionError` subclass, so it escapes uncaught and crashes the
# worker process in a restart loop. Disabling the client-side timeout here
# leaves BRPOP's indefinite server-side block as the only wait, which is what
# this broker actually expects.
broker = ListQueueBroker(url=_settings.redis.url, socket_timeout=None).with_result_backend(
    RedisAsyncResultBackend(redis_url=_settings.redis.url),
)

scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])

_container: AsyncContainer | None = None


def get_container() -> AsyncContainer:
    """Container for task bodies. Built once per worker process."""
    if _container is None:
        msg = "container is not initialised - tasks must run inside a taskiq worker"
        raise RuntimeError(msg)
    return _container


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    global _container  # noqa: PLW0603 - one container per worker process
    settings = get_settings()
    setup_logging(settings)
    _container = create_container(settings)
    state.container = _container


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:  # noqa: ARG001 - signature fixed by the event hook
    global _container  # noqa: PLW0603
    if _container is not None:
        await _container.close()
        _container = None


from bot.tasks import analytics, broadcast, export, notify, payments  # noqa: E402, F401 - registers tasks on the broker
