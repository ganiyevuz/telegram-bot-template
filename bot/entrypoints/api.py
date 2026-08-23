from __future__ import annotations
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from loguru import logger

from bot.api import health, metrics, webhook
from bot.core.config import get_settings
from bot.core.lifespan import lifespan as app_lifespan
from bot.keyboards.default_commands import set_default_commands

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    async with app_lifespan(settings) as ctx:
        app.state.ctx = ctx
        await set_default_commands(ctx.bot)
        if settings.webhook.enabled:
            await ctx.bot.set_webhook(
                settings.webhook.url,
                allowed_updates=ctx.dp.resolve_used_update_types(),
                # `.secret` is a SecretStr; aiogram's `set_webhook` needs the plain
                # value — passing the SecretStr itself would send its masked repr.
                secret_token=settings.webhook.secret.get_secret_value() or None,
                drop_pending_updates=False,
            )
            logger.info(f"webhook registered | {settings.webhook.url}")
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="Telegram Bot", lifespan=_lifespan, docs_url=None, redoc_url=None)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(webhook.router)
    return app


app = create_app()
