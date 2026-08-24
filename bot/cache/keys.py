from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

NAMESPACE = "tpl"
VERSION = "v1"


class CacheKeys:
    """Single source of truth for cache keys.

    Every key is versioned, so bumping VERSION invalidates the whole namespace
    on deploy without touching Redis.
    """

    @staticmethod
    def _key(*parts: str | int) -> str:
        return ":".join((NAMESPACE, VERSION, *(str(p) for p in parts)))

    @classmethod
    def user_exists(cls, user_id: int) -> str:
        return cls._key("user", user_id, "exists")

    @classmethod
    def user_language(cls, user_id: int) -> str:
        return cls._key("user", user_id, "language")

    @classmethod
    def user_first_name(cls, user_id: int) -> str:
        return cls._key("user", user_id, "first_name")

    @classmethod
    def user_is_admin(cls, user_id: int) -> str:
        return cls._key("user", user_id, "is_admin")

    @classmethod
    def user_count(cls) -> str:
        return cls._key("user", "count")

    @classmethod
    def for_user(cls, user_id: int) -> list[str]:
        """Every per-user key. Writes invalidate this whole list."""
        return [
            cls.user_exists(user_id),
            cls.user_language(user_id),
            cls.user_first_name(user_id),
            cls.user_is_admin(user_id),
        ]

    @classmethod
    def dedup(cls, update_id: int) -> str:
        return cls._key("dedup", update_id)

    @classmethod
    def throttle(cls, scope: str, ident: int | str) -> str:
        return cls._key("throttle", scope, ident)

    @classmethod
    def broadcast(cls, broadcast_id: str) -> str:
        return cls._key("broadcast", broadcast_id)

    @classmethod
    def outbound(cls, scope: str, ident: int | str) -> str:
        return cls._key("outbound", scope, ident)

    @classmethod
    def notify(cls, fingerprint: str) -> str:
        """Cooldown marker. Present = an alert with this fingerprint was sent recently."""
        return cls._key("notify", fingerprint)

    @classmethod
    def notify_count(cls, fingerprint: str) -> str:
        """How many alerts this fingerprint swallowed during the current cooldown."""
        return cls._key("notify", fingerprint, "count")

    @classmethod
    def backup(cls, day: date) -> str:
        """Telegram message ids for the backup shipped on `day`, oldest push first.

        This is the retention sweep's ONLY source of truth about what it may delete. The
        backup channel is never listed or scanned: anything in there this bot did not
        record belongs to somebody else.
        """
        return cls._key("backup", day.isoformat())

    @classmethod
    def backup_days(cls) -> str:
        """Index of the days that still have message ids on record.

        Every `backup()` key carries a TTL, so without an index the only way to find them
        again would be a SCAN across a keyspace this Redis shares with FSM state, the
        caches and the throttle buckets. This set is one SMEMBERS instead, and it stays
        small because the sweep drops a day as soon as that day's messages are gone.
        """
        return cls._key("backup", "days")
