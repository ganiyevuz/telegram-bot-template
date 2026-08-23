from __future__ import annotations
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from dishka.integrations.fastapi import ContainerMiddleware
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from bot.api import health, metrics, webhook
from bot.core.config import BOT_DIR, get_settings
from bot.core.lifespan import lifespan as app_lifespan
from bot.keyboards.default_commands import set_default_commands
from bot.webapp import routes as webapp_routes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# The bundled demo Mini App page; replace its contents with your own front end.
WEBAPP_STATIC_DIR = f"{BOT_DIR}/webapp/static"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.webhook.enabled and not settings.webhook.secret.get_secret_value():
        # Fail loud at startup, not silently at request time: the route's own
        # secret check (`if secret and ...`) only rejects requests once a secret
        # IS configured — an empty secret skips it entirely. WEBHOOK_VERIFY_SOURCE_IP
        # is NOT accepted as a substitute here: webhook mode requires HTTPS, so
        # there is always a proxy/TLS terminator in front of this process. With
        # `trust_proxy_headers=False`, `request.client.host` is that proxy's own
        # address — never one of Telegram's — so the check would reject 100% of
        # genuine traffic, not guard anything. With `trust_proxy_headers=True`,
        # the check is only as trustworthy as that proxy stripping a
        # client-supplied `X-Forwarded-For`, which is infrastructure this process
        # cannot verify. The secret token is free, always available, and is
        # Telegram's own recommended mechanism — an empty WEBHOOK_SECRET means
        # anyone can forge updates as any Telegram user. A warning buried in logs
        # during an incident is easy to miss; a process that refuses to start is
        # not.
        msg = (
            "WEBHOOK_SECRET must be set when USE_WEBHOOK=True — the webhook endpoint "
            "would otherwise accept forged updates from anyone"
        )
        raise RuntimeError(msg)
    app.state.settings = settings
    async with app_lifespan(settings) as ctx:
        app.state.ctx = ctx
        # The other half of the dishka wiring `create_app()` started — see the comment
        # there for why `setup_dishka(container, app)` cannot be called as one piece.
        # `ContainerMiddleware` reads this attribute on every HTTP request to open a
        # REQUEST-scoped child container, so it must be set before the first request,
        # and the container only exists from here on.
        app.state.dishka_container = ctx.container
        # `setup_dishka(..., auto_inject=True)` (bot/core/lifespan.py) defers all
        # `FromDishka[...]` wiring to a `router.startup` hook — see
        # dishka/integrations/aiogram.py's `setup_dishka`/`inject_router`.
        # `Dispatcher.start_polling()` (bot/entrypoints/polling.py) triggers that
        # hook internally via its own `emit_startup()` call; `feed_update()` alone —
        # what the webhook route below calls on every request — never does. Without
        # this, every handler that injects a dishka dependency (e.g. support.py's
        # support_handler, callbacks.py's support_callback) raises `TypeError: ...
        # missing ... argument: 'settings'` on its first call — silently, since
        # bot/handlers/errors.py's error handler swallows it and the webhook still
        # returns 200.
        await ctx.dp.emit_startup(bot=ctx.bot)
        await set_default_commands(ctx.bot, ctx.i18n)
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
        # Symmetric with start_polling(), which calls `emit_shutdown()` in its
        # `finally` block. `Dispatcher.__init__` always registers `self.fsm.close`
        # on `dp.shutdown`, so without this the FSM storage's shutdown hook never
        # runs under webhook mode. Must happen before this `async with` block exits
        # below — that is what triggers `app_lifespan`'s own `container.close()`
        # (bot/core/lifespan.py), which tears down the same Redis client
        # `RedisStorage.close()` needs to still be alive.
        await ctx.dp.emit_shutdown(bot=ctx.bot)


async def _webapp_index() -> FileResponse:
    return FileResponse(f"{WEBAPP_STATIC_DIR}/index.html")


def create_app() -> FastAPI:
    settings = get_settings()
    # `openapi_url=None` alongside the two UI routes: leaving the schema served while
    # disabling /docs and /redoc hands anyone the full route table — `/webhook`
    # included — on an origin that Tasks 9 and 10 publish to every user and register
    # with BotFather. Set it back to "/openapi.json" behind an authenticated proxy if
    # you need the schema.
    app = FastAPI(title="Telegram Bot", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # `dishka.integrations.fastapi.setup_dishka(container, app)` is exactly two
    # statements — this `add_middleware` call plus `app.state.dishka_container = ...` —
    # and it cannot be used as one piece here: the container does not exist until
    # `app_lifespan` has started, but Starlette freezes the middleware stack *before*
    # running the lifespan, so calling it there raises `RuntimeError: Cannot add
    # middleware after an application has started`. Hence the split: the middleware is
    # registered at construction time, the container attached in `_lifespan` above.
    # (The aiogram side is wired separately, in bot/core/lifespan.py.)
    app.add_middleware(ContainerMiddleware)
    app.include_router(health.router)
    app.include_router(metrics.router)
    if settings.webhook.enabled:
        # Mounted ONLY in webhook mode. In polling mode the route has no purpose —
        # `bot.entrypoints.polling` is what feeds updates — while the process may still
        # be running this app to serve the Mini App over the HTTPS origin Telegram
        # requires. An endpoint that does not exist cannot be misconfigured; the
        # `_lifespan` guard above and the route's own secret check only cover the
        # `webhook.enabled` case, so without this the polling operator gets a live,
        # unauthenticated update sink whenever WEBHOOK_SECRET is empty.
        app.include_router(webhook.router)
    app.include_router(webapp_routes.router)
    # `html=True` makes the mount serve `index.html` for a bare directory request.
    # The explicit `/webapp` route in front of it is not redundant: Starlette compiles a
    # mount at `/webapp` to `^/webapp/(?P<path>.*)$`, so the bare path matches nothing and
    # falls through to `redirect_slashes`, answering the Mini App's own entry URL — the one
    # configured in BotFather and passed to `WebAppInfo(url=...)` — with a 307 to
    # `/webapp/` instead of the page.
    app.add_api_route("/webapp", _webapp_index, methods=["GET"], include_in_schema=False)
    app.mount("/webapp", StaticFiles(directory=WEBAPP_STATIC_DIR, html=True), name="webapp")
    return app


app = create_app()
