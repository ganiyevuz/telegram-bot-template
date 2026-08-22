from __future__ import annotations

import uvloop

from bot.core.config import get_settings
from bot.core.lifespan import lifespan
from bot.keyboards.default_commands import remove_default_commands, set_default_commands


async def main() -> None:
    """Development entrypoint.

    Telegram allows only one polling process per token, so this is never a
    production path — use `bot.entrypoints.api` instead.
    """
    settings = get_settings()
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
