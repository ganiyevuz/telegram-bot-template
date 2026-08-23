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
    if settings.webhook.enabled and not settings.webhook.secret.get_secret_value():
        # Fail loud at startup, not silently at request time: the route's own
        # secret check (`if secret and ...`) only rejects requests once a secret
        # IS configured — an empty secret skips it entirely, and combined with
        # source-IP verification being spoofable without a trusted proxy, an
        # empty WEBHOOK_SECRET means anyone can forge updates as any Telegram
        # user. A warning buried in logs during an incident is easy to miss; a
        # process that refuses to start is not.
        msg = (
            "WEBHOOK_SECRET must be set when USE_WEBHOOK=True — the webhook endpoint "
            "would otherwise accept forged updates from anyone"
        )
        raise RuntimeError(msg)
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
