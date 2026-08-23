from __future__ import annotations
import hashlib
import hmac
import secrets

# scrypt parameters. n=2**14 with r=8, p=1 is the interactive-login profile from RFC 7914
# and costs roughly 100 ms per hash — slow enough to make offline guessing expensive,
# fast enough that a login does not feel broken.
_N = 2**14
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_LEN = 64


def hash_password(password: str) -> str:
    """A `scrypt$<salt-hex>$<digest-hex>` string, safe to store verbatim.

    stdlib scrypt rather than a dependency: passlib and argon2 would each add a tree
    for one function, and the spec is explicit that this must not be hand-rolled —
    which it is not, since `hashlib.scrypt` is the primitive.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_LEN)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Returns False for anything malformed.

    `hmac.compare_digest`, not `==`: a plain comparison short-circuits on the first
    differing byte and leaks the digest to a timing attack. This project has already
    fixed that exact bug twice — on the webhook secret and on the Mini App initData
    signature — so it is not a hypothetical here.
    """
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    if not expected:
        # `hashlib.scrypt` rejects `dklen=0` with a ValueError, so `scrypt$<salt>$`
        # would escape as an exception rather than a False. The docstring promises
        # False for anything malformed, and a 500 on the login form is a worse
        # answer than a rejected password.
        return False
    candidate = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=len(expected))
    return hmac.compare_digest(candidate, expected)
