# ruff: noqa: RUF001 - the INFO emoji below is intentionally a Unicode symbol, not the ASCII look-alike
from __future__ import annotations
import enum
import html


class AlertLevel(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def emoji(self) -> str:
        return {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.ERROR: "🔥",
            AlertLevel.CRITICAL: "🚨",
        }[self]

    def format(self, title: str, body: str, suppressed: int = 0) -> str:  # type: ignore[override]
        """The message text. Everything caller-supplied is HTML-escaped.

        Shadows `str.format` deliberately — `AlertLevel` is a `StrEnum` so it inherits
        that method, but nothing in this codebase calls an `AlertLevel` value's
        `str.format`; this is the notifier's message-rendering entry point instead.

        The bot's default parse mode is HTML (bot/telegram/factory.py), and both `title`
        and `body` can carry an exception message, a URL, or whatever a caller POSTed to
        /api/notify. An unescaped `<` makes Telegram reject the entire message — so the
        alert about a failure would itself fail, silently, exactly when it is needed.
        """
        tail = f"\n\n<i>(x{suppressed} suppressed)</i>" if suppressed else ""
        return f"{self.emoji} <b>{html.escape(title)}</b>\n<pre>{html.escape(body)}</pre>{tail}"
