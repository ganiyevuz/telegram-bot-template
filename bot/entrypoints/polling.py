from __future__ import annotations

import uvloop
from loguru import logger

from bot.core.config import get_settings
from bot.core.lifespan import lifespan
from bot.keyboards.default_commands import remove_default_commands, set_default_commands


async def main() -> None:
    """Development entrypoint.

    Telegram allows only one update-delivery mechanism per token at a time, so
    this refuses to start when `USE_WEBHOOK=True`. `bot.entrypoints.api` is the
    production path in that mode, and every replica of it holds a registered
    webhook; polling alongside it would not degrade gracefully, it would spin in
    a 409 Conflict loop (`getUpdates` is rejected outright while a webhook is
    set) and deliver nothing. Failing fast beats a warning here: a log line is
    easy to miss in an automated start, a nonzero exit is not.
    """
    settings = get_settings()
    if settings.webhook.enabled:
        logger.error(
            "USE_WEBHOOK=True — refusing to start the polling entrypoint "
            "(it would only 409-loop against the registered webhook); run bot.entrypoints.api instead",
        )
        raise SystemExit(1)

    async with lifespan(settings) as ctx:
        await ctx.bot.delete_webhook(drop_pending_updates=False)
        await set_default_commands(ctx.bot)
        try:
            # The container owns the Bot, so it — not the dispatcher — closes its session.
            await ctx.dp.start_polling(
                ctx.bot,
                allowed_updates=ctx.dp.resolve_used_update_types(),
                close_bot_session=False,
            )
        finally:
            await remove_default_commands(ctx.bot)


def run() -> None:
    uvloop.run(main())


if __name__ == "__main__":
    run()
