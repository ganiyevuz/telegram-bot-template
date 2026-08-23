from __future__ import annotations
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from pydantic import BaseModel


class InvalidInitData(Exception):  # noqa: N818 - name is a fixed contract from the phase 9 plan
    """The signature did not verify — the data is forged or the token is wrong."""


class ExpiredInitData(Exception):  # noqa: N818 - name is a fixed contract from the phase 9 plan
    """The signature verified but the data is older than we accept."""


class WebAppUser(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool = False


def validate_init_data(init_data: str, bot_token: str, max_age: timedelta) -> WebAppUser:
    """Verify Telegram Mini App initData and return the user it names.

    Two-stage HMAC, per Telegram's spec: the bot token is the MESSAGE in the
    first stage (keyed by the literal b"WebAppData"), and the resulting digest
    is the KEY for the second stage over the sorted data-check string.
    """
    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received = parsed.pop("hash", "")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    # Constant-time: `==` short-circuits on the first differing byte and leaks the
    # signature to a timing attack. This project already learned that the hard way
    # on the webhook secret — see the phase 1-6 execution log.
    if not hmac.compare_digest(expected, received):
        raise InvalidInitData

    # Without this, a captured initData string is replayable forever.
    issued = datetime.fromtimestamp(int(parsed["auth_date"]), tz=UTC)
    if datetime.now(UTC) - issued > max_age:
        raise ExpiredInitData

    return WebAppUser.model_validate_json(parsed["user"])
