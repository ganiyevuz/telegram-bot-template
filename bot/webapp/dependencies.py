# ruff: noqa: TC001  - FastAPI and dishka resolve this dependency's signature at
# runtime via get_type_hints(); these imports must stay at module level
import math
from datetime import timedelta
from typing import Annotated, NoReturn

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Header, HTTPException, status
from loguru import logger
from redis.exceptions import RedisError

from bot.cache.keys import CacheKeys
from bot.core.config import Settings
from bot.core.di import ThrottleBucket
from bot.middlewares.metrics import THROTTLED, WEBAPP_INIT_DATA_REJECTIONS
from bot.webapp.initdata import ExpiredInitData, InvalidInitData, WebAppUser, validate_init_data

# The scheme Telegram documents for authenticating Mini App requests:
# `Authorization: tma <initData>`.
SCHEME = "tma"

# One body for every rejection reason. Telling a caller whether their forgery failed on
# the signature or on the clock hands a prober a free oracle; the distinction is logged
# and counted server-side instead, where only we can read it.
UNAUTHORIZED_DETAIL = "unauthorized"

# The throttle scope for this API. `CacheKeys.throttle(scope, id)` namespaces by scope,
# so Mini App calls draw on their own bucket rather than sharing the one
# `ThrottlingMiddleware` uses for incoming updates — a user browsing the page should not
# spend the allowance that delivers their messages, or vice versa.
THROTTLE_SCOPE = "webapp"


def _reject(reason: str, detail: str, cause: Exception | None = None) -> NoReturn:
    """Record why this request was rejected, then raise the 401 that says none of it.

    `cause` is threaded through explicitly so the original validation error stays
    attached (`raise ... from`) even though the raise happens one frame down.
    """
    WEBAPP_INIT_DATA_REJECTIONS.labels(reason=reason).inc()
    logger.warning(f"webapp request rejected | reason: {reason} | {detail}")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHORIZED_DETAIL) from cause


async def _throttle(bucket: ThrottleBucket, user_id: int) -> None:
    """Spend one token for this caller, or refuse the request with a 429.

    Applied here rather than per route so every current and future webapp endpoint
    inherits it, and keyed on the *verified* `WebAppUser.id` — the only identity this
    API has, and one an attacker cannot pick. Without it a single captured `initData`
    is an unlimited credential against `POST /api/webapp/invoice`: each call is a
    `create_invoice_link` to Telegram, and `OutboundRateLimiter` *waits* on the shared
    global 30/s bucket rather than shedding, so a flood here delays ordinary message
    delivery for every user on every replica.
    """
    try:
        wait = await bucket.acquire(CacheKeys.throttle(THROTTLE_SCOPE, user_id))
    except RedisError as exc:
        # Fail OPEN, the same call `ThrottlingMiddleware` makes for the identical case
        # and for the same reason: Redis being unreachable means we cannot tell whether
        # this caller is over their limit, and refusing every Mini App request during a
        # Redis blip is worse than serving them unthrottled. A deployment that needs a
        # hard quota here should re-raise instead.
        logger.warning(f"webapp throttle unavailable, serving without it | user_id: {user_id} | error: {exc}")
        return
    if wait > 0:
        logger.debug(f"webapp throttled | user_id: {user_id}")
        THROTTLED.labels(scope=THROTTLE_SCOPE).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many requests",
            # The bucket already computed exactly how long until a token is free, so the
            # client is told rather than left to guess. Ceil: `Retry-After` is integer
            # seconds, and rounding down would invite an immediate second rejection.
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )


@inject
async def webapp_user(
    settings: FromDishka[Settings],
    bucket: FromDishka[ThrottleBucket],
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
        user = validate_init_data(
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

    # Signature first, throttle second: the bucket is keyed by user id, so it must be an
    # id we have proven rather than one the caller asserted — otherwise anyone could
    # exhaust another user's allowance, or spread their own flood across made-up ids.
    await _throttle(bucket, user.id)
    return user


# Every webapp route takes the caller this way. There is deliberately no user-id
# parameter anywhere in this API: identity comes from the verified signature and
# nowhere else, so there is no route left for somebody to forget to authorise.
CurrentWebAppUser = Annotated[WebAppUser, Depends(webapp_user)]
