from __future__ import annotations
import logging
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from types import FrameType

    from bot.core.config import Settings

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def bind_correlation_id(value: str) -> None:
    """Bind an identifier that every later log line in this task will carry."""
    correlation_id.set(value)


def _patch(record: dict[str, Any]) -> None:
    record["extra"]["correlation_id"] = correlation_id.get() or "-"


class InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn, sqlalchemy, taskiq) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(settings: Settings) -> None:
    logger.remove()
    logger.configure(patcher=_patch)  # type: ignore[arg-type]
    logger.add(
        sys.stdout,
        level=settings.observability.log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "cid={extra[correlation_id]} | {name}:{function}:{line} | {message}"
        ),
        backtrace=settings.debug,
        diagnose=settings.debug,
    )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "taskiq", "aiogram"):
        stdlib = logging.getLogger(name)
        stdlib.handlers = [InterceptHandler()]
        stdlib.propagate = False
