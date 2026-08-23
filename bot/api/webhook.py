from __future__ import annotations
import ipaddress
from typing import TYPE_CHECKING, Annotated

from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request, status
from loguru import logger

from bot.core.logging import correlation_id

if TYPE_CHECKING:
    from bot.core.config import Settings

# Telegram's published webhook source ranges.
TELEGRAM_SUBNETS = (
    ipaddress.ip_network("149.154.160.0/20"),
    ipaddress.ip_network("91.108.4.0/22"),
)

router = APIRouter(tags=["telegram"])


def _from_telegram(client_ip: str) -> bool:
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(address in subnet for subnet in TELEGRAM_SUBNETS)


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str, Header()] = "",
) -> dict[str, bool]:
    ctx = request.app.state.ctx
    settings: Settings = request.app.state.settings

    # `.secret` is a SecretStr — comparing it to the header directly would never
    # match (SecretStr.__eq__ only matches another SecretStr), silently rejecting
    # every request once a secret is configured.
    secret = settings.webhook.secret.get_secret_value()
    if secret and x_telegram_bot_api_secret_token != secret:
        logger.warning("webhook rejected | bad secret token")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    if settings.webhook.verify_source_ip:
        client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else ""
        )
        if not _from_telegram(client_ip):
            logger.warning(f"webhook rejected | source ip not Telegram: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    update = Update.model_validate(await request.json(), context={"bot": ctx.bot})

    try:
        await ctx.dp.feed_update(ctx.bot, update)
    except Exception as exc:  # noqa: BLE001 - last-resort guard around the whole dispatch chain
        # Handler exceptions never reach here — aiogram's own error boundary inside
        # `Router.propagate_event` catches those and routes them to
        # bot/handlers/errors.py, which always returns True. What DOES reach here
        # is a failure in an outer middleware itself: aiogram's FSMContextMiddleware
        # queries Redis before any middleware registered in bot/middlewares/__init__.py
        # runs, so a Redis outage raises here reliably, uncaught by anything upstream.
        # Left alone this becomes an HTTP 500, and Telegram retries a 500 — turning a
        # dependency outage into a retry storm layered on top of it. We return 200
        # instead: the same fail-open call DedupMiddleware and ThrottlingMiddleware
        # make for the identical Redis-down case (see their docstrings). The update is
        # lost, which is the lesser evil. A bot that cannot tolerate losing an update
        # should return 503 here instead, to make Telegram retry once the dependency
        # recovers — at the cost of a retry storm during the outage.
        cid = correlation_id.get() or str(update.update_id)
        logger.opt(exception=exc).error(f"webhook feed_update failed | cid: {cid}")
        return {"ok": False}

    return {"ok": True}
