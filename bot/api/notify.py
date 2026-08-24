# ruff: noqa: TC001 - FastAPI and dishka resolve this route's signature at
# runtime via get_type_hints(); these imports must stay at module level
import hashlib
import hmac
import time
from typing import Annotated, NoReturn

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from bot.core.config import Settings
from bot.middlewares.metrics import NOTIFY_REJECTIONS
from bot.notifier import AlertLevel, NotifierService

# How far `X-Timestamp` may be from our own clock, in EITHER direction. Behind is the
# replay window: a captured request is only good for this long. Ahead matters just as
# much — accepting a far-future timestamp would let one capture be replayed for as long
# as the attacker cared to set it, which is the whole thing the header exists to bound.
MAX_SKEW_SECONDS = 300

# Byte-identical to the body Starlette returns for a route that does not exist. An
# unconfigured notifier answers 404 rather than 401 precisely so a prober cannot tell
# the two apart — a distinct body would hand back the fact the endpoint is there,
# unconfigured, waiting for someone to set NOTIFIER_SECRET.
NOT_FOUND_DETAIL = "Not Found"

# One body for every authentication failure, the same call bot/webapp/dependencies.py
# makes: telling a caller whether they failed on the clock or on the signature is a free
# oracle. The distinction is counted and logged server-side, where only we can read it.
UNAUTHORIZED_DETAIL = "unauthorized"

router = APIRouter(prefix="/api", tags=["notifier"], route_class=DishkaRoute)


class NotifyRequest(BaseModel):
    """One inbound alert. Every field is caller-supplied and none of it is ever executed
    or interpolated into anything but the message text, which `AlertLevel.render`
    HTML-escapes. The lengths are bounds, not validation: Telegram caps a message at 4096
    characters, and escaping only makes the rendered text longer than what arrives here.
    """

    level: AlertLevel
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=3000)
    source: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=64)


class NotifyResponse(BaseModel):
    # False means an identical alert is already inside its cooldown, so this one was
    # folded into that window's suppressed count instead of being delivered. Still a 202:
    # the alert was accepted, and suppression is the notifier working as designed.
    queued: bool


def _reject(reason: str, message: str) -> NoReturn:
    """Count why this request was refused, then raise the 401 that says none of it."""
    NOTIFY_REJECTIONS.labels(reason=reason).inc()
    logger.warning(f"notify rejected | reason: {reason} | {message}")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHORIZED_DETAIL)


@router.post("/notify", status_code=status.HTTP_202_ACCEPTED)
async def notify(
    request: Request,
    notifier: FromDishka[NotifierService],
    settings: FromDishka[Settings],
    x_signature: Annotated[str, Header()] = "",
    x_timestamp: Annotated[str, Header()] = "",
) -> NotifyResponse:
    """Accept an alert from CI, cron, an uptime check — anything outside this process.

    The only public write surface the bot exposes. It authenticates with an HMAC-SHA256
    over the raw request body keyed by `NOTIFIER_SECRET`, enqueues, and returns 202; it
    never blocks on Telegram and never does anything with the payload except render it as
    text. Order of checks below is deliberate — see each one.

    Replaying a valid request inside the freshness window returns 202 again, by design.
    Idempotency lives in `NotifierService`'s fingerprint cooldown, which folds the repeat
    into a suppressed count; making this endpoint consume signatures would need a
    server-side store of every signature seen, to buy a property the layer below already
    provides.
    """
    secret = settings.notifier.secret.get_secret_value()
    if not secret:
        # 404, not 401. With no secret there is nothing to authenticate against, and an
        # endpoint nobody has configured should not advertise that it exists. Logged at
        # debug, not warning: an unconfigured notifier is a choice (same call
        # bot/tasks/notify.py makes), and a probe of a disabled endpoint is not an
        # incident worth waking anyone for.
        NOTIFY_REJECTIONS.labels(reason="disabled").inc()
        logger.debug("notify rejected | reason: disabled | NOTIFIER_SECRET is not set")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    # Clock before signature: a stale request is not worth an HMAC, and a missing header
    # is the same answer as an unparseable one.
    try:
        sent_at = int(x_timestamp)
    except ValueError:
        _reject("stale", "X-Timestamp missing or not an integer")
    if abs(time.time() - sent_at) > MAX_SKEW_SECONDS:
        _reject("stale", f"X-Timestamp {sent_at} is outside the +/-{MAX_SKEW_SECONDS}s window")

    # The RAW bytes, deliberately — not the parsed model. Re-serialising pydantic output
    # will not reproduce the caller's whitespace, key order or number formatting, so an
    # HMAC over it would never verify against one computed by the caller.
    raw = await request.body()
    # The timestamp is signed WITH the body, not merely sent alongside it. Signing the
    # body alone would leave the header free to edit, so a single captured request could
    # be replayed forever by moving its timestamp forward. `x_timestamp` verbatim rather
    # than the parsed `sent_at`: the caller signed the string they sent, and re-rendering
    # an int would break any caller who zero-pads it.
    expected = hmac.new(secret.encode(), f"{x_timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    # `compare_digest`, never `==`. A plain compare short-circuits on the first differing
    # byte, which leaks the expected digest one byte at a time to anyone who can time the
    # response — the same bug already fixed on the webhook secret token and on the Mini
    # App initData. Compared as bytes: a header carrying a non-ASCII character makes
    # `compare_digest` raise TypeError on str inputs, turning a forgery attempt into a
    # 500. `.lower()` only widens which spellings of the same hex digest are accepted.
    if not hmac.compare_digest(x_signature.strip().lower().encode(), expected.encode()):
        _reject("signature", "X-Signature did not verify")

    # Validated by hand, after authentication, rather than declared as a route parameter:
    # FastAPI resolves body parameters BEFORE the function runs, so a model parameter
    # would answer an unauthenticated caller with a 422 that spells out the schema — and
    # would do it even when the endpoint is meant to be a 404. `RequestValidationError`
    # keeps the framework's own 422 shape for the callers who do get this far.
    try:
        payload = NotifyRequest.model_validate_json(raw)
    except ValidationError as exc:
        NOTIFY_REJECTIONS.labels(reason="payload").inc()
        logger.warning(f"notify rejected | reason: payload | body did not match the schema ({exc.error_count()})")
        raise RequestValidationError(exc.errors(include_url=False)) from exc

    # The spec's fingerprint rule for inbound alerts: source and key. Both are bounded
    # strings from the payload, so a caller controls how its own alerts group and cannot
    # affect anybody else's — the fingerprint never reaches a metric label.
    queued = await notifier.send(payload.level, payload.title, payload.body, f"{payload.source}:{payload.key}")
    return NotifyResponse(queued=queued)
