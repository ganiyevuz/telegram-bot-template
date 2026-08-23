# ruff: noqa: TC001  - FastAPI and dishka resolve this dependency's signature at
# runtime via get_type_hints(); these imports must stay at module level
from datetime import timedelta
from typing import Annotated, NoReturn

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Header, HTTPException, status
from loguru import logger

from bot.core.config import Settings
from bot.middlewares.metrics import WEBAPP_INIT_DATA_REJECTIONS
from bot.webapp.initdata import ExpiredInitData, InvalidInitData, WebAppUser, validate_init_data

# The scheme Telegram documents for authenticating Mini App requests:
# `Authorization: tma <initData>`.
SCHEME = "tma"

# One body for every rejection reason. Telling a caller whether their forgery failed on
# the signature or on the clock hands a prober a free oracle; the distinction is logged
# and counted server-side instead, where only we can read it.
UNAUTHORIZED_DETAIL = "unauthorized"


def _reject(reason: str, detail: str, cause: Exception | None = None) -> NoReturn:
    """Record why this request was rejected, then raise the 401 that says none of it.

    `cause` is threaded through explicitly so the original validation error stays
    attached (`raise ... from`) even though the raise happens one frame down.
    """
    WEBAPP_INIT_DATA_REJECTIONS.labels(reason=reason).inc()
    logger.warning(f"webapp request rejected | reason: {reason} | {detail}")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHORIZED_DETAIL) from cause


@inject
async def webapp_user(
    settings: FromDishka[Settings],
    authorization: Annotated[str | None, Header()] = None,
) -> WebAppUser:
    """The caller, proven by the `initData` signature on this request.

    `initData` is revalidated on every request rather than exchanged for a session
    token: the HMAC is cheap, Telegram refreshes `initData` client-side, and it keeps
    every API replica stateless — no shared session store, no token lifecycle, no
    revocation problem.
    """
    scheme, _, init_data = (authorization or "").partition(" ")
    if scheme.lower() != SCHEME or not init_data:
        _reject("missing", "no `Authorization: tma <initData>` header")

    try:
        return validate_init_data(
            init_data,
            # `.token` is a SecretStr; the plain value is the HMAC message. Passing the
            # SecretStr itself would sign against its masked repr and reject everything.
            settings.bot.token.get_secret_value(),
            timedelta(seconds=settings.webapp.init_data_max_age_seconds),
        )
    except ExpiredInitData as exc:
        _reject("expired", "initData older than WEBAPP_INIT_DATA_MAX_AGE", exc)
    except InvalidInitData as exc:
        _reject("invalid", "initData signature did not verify", exc)


# Every webapp route takes the caller this way. There is deliberately no user-id
# parameter anywhere in this API: identity comes from the verified signature and
# nowhere else, so there is no route left for somebody to forget to authorise.
CurrentWebAppUser = Annotated[WebAppUser, Depends(webapp_user)]
