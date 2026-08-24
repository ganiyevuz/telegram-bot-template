"""Backupgram's two moving parts: ship the encrypted dump, then sweep what has aged out.

`BackupService` (bot/services/backup.py) produces the ciphertext; this module is what puts
it on Telegram and what takes it off again. Both halves are tasks rather than handler code
deliberately — a multi-part upload of a whole database runs for minutes, several orders of
magnitude past the window an aiogram handler has, and it would block the update pipeline
for every other user while it ran.
"""

from __future__ import annotations
import html
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import orjson
import pycron
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile
from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import RedisError

from bot.cache.keys import CacheKeys
from bot.core.config import BackupSettings, Settings, get_settings
from bot.notifier import AlertLevel, NotifierService
from bot.services.backup import BackupError, BackupService
from bot.tasks import broker, get_container

if TYPE_CHECKING:
    from bot.services.backup import BackupArtifact

# aiogram's session timeout is 60s. A 45 MiB part needs more than that on anything short of
# a fast uplink (60s is ~6 Mbit/s just for one part), and a timeout here does not merely
# retry — it abandons a half-finished upload and burns the whole backup run. Ten minutes per
# part is generous rather than tight on purpose: the cost of waiting is nothing, the cost of
# giving up early is a night without a backup.
UPLOAD_TIMEOUT = 600
# How long the message-id record outlives the retention window. The sweep has to still find
# the ids on the day it decides those messages have aged out, so the record must not expire
# first — and a worker that was down over a weekend must not turn a backup into messages
# nothing is able to delete any more.
RETENTION_GRACE_DAYS = 7
_CRON_FIELDS = 5


def _resolve_schedule() -> str:
    """The cron `backup:run` is scheduled on, read from settings at import.

    `LabelScheduleSource.startup()` reads `task.labels["schedule"]` in the *scheduler*
    process, so the value only has to be correct by the time this module is imported — and
    by then `get_settings()` has already read `.env`, exactly as `bot/tasks/__init__.py`
    relies on for the broker URL. BACKUP_SCHEDULE therefore genuinely takes effect; this is
    not a literal frozen at authoring time.

    Validated with `pycron`, which is taskiq's own cron parser and a hard dependency of it,
    rather than with a hand-rolled check that could disagree with it. An expression taskiq
    cannot parse does not crash the scheduler: it logs one stdlib warning a minute and the
    task never fires. "The backups quietly stopped" is the single worst outcome this feature
    has, so a malformed value falls back to the documented default, loudly.

    The limit of that check is pycron's: it raises on the wrong number of fields (`@daily`,
    a missing field, a stray space) but parses field *contents* leniently, so an expression
    like `"x y z * *"` is accepted here and then simply never matches. Field count is where
    the realistic typos live; anything past it is taskiq's parser to own, not ours.
    """
    configured = get_settings().backup.schedule
    fallback = str(BackupSettings.model_fields["schedule"].default)
    try:
        pycron.is_now(configured)
    except ValueError as exc:
        logger.error(
            f"BACKUP_SCHEDULE is not a valid cron expression, falling back to {fallback!r} | "
            f"value: {configured!r} | error: {exc} | expected {_CRON_FIELDS} space-separated fields "
            f"(minute hour day-of-month month day-of-week)",
        )
        return fallback
    return configured


@broker.task(task_name="backup:run", schedule=[{"cron": _resolve_schedule()}])
async def run_backup(report_chat_id: int | None = None) -> int:
    """Encrypt the newest dump and ship it to BACKUP_CHAT_ID. Returns the parts delivered.

    `report_chat_id` is set when an admin ran /backup: the outcome is echoed there as well
    as to the notifier, because the handler promised a report in that chat while the alerts
    channel is somewhere else — or, by default, nowhere at all.

    Never raises. A task that raises is retried, and retrying a failed multi-part upload
    turns one bad night into a loop, usually while the thing that broke is still broken.
    """
    container = get_container()
    settings = await container.get(Settings)
    notifier = await container.get(NotifierService)
    bot = await container.get(Bot)

    chat_id = settings.backup.chat_id
    if chat_id is None:
        # Unset is this feature's off switch, the same one NotifierSettings.chat_id uses,
        # and an off switch is a choice rather than a fault — no alert, or every deployment
        # that never turned backups on would be nagged nightly about it. An admin who typed
        # /backup does get told, because they are waiting for an answer.
        logger.debug("backup skipped | BACKUP_CHAT_ID is not set")
        await _report(bot, report_chat_id, "Backups are switched off: BACKUP_CHAT_ID is not set.")
        return 0

    backup = await container.get(BackupService)
    try:
        artifact = await backup.prepare()
    except BackupError as exc:
        # Nothing has gone out at this point and nothing will: `prepare()` raises
        # BackupNotConfigured before it reads a single byte when there is no age recipient.
        # That ordering is the whole safety property — this is the path that must never fall
        # through to an upload, because falling through means a plaintext database dump on
        # Telegram. Note `BackupError`, not `Exception`: an unexpected failure type here
        # should surface as a task error, not be quietly summarised as "backup failed".
        await _fail(bot, notifier, report_chat_id, exc)
        return 0

    redis = await container.get(Redis)
    day = datetime.now(UTC).date()
    sent: list[int] = []
    try:
        await _upload(bot, chat_id, artifact, sent)
    except TelegramAPIError as exc:
        await _fail(bot, notifier, report_chat_id, exc)
        return 0
    else:
        summary = (
            f"{artifact.source_name} | parts: {artifact.part_count} | "
            f"{artifact.total_bytes} bytes encrypted | sha256: {artifact.sha256}"
        )
        logger.info(f"backup shipped | chat_id: {chat_id} | {summary}")
        await notifier.send(AlertLevel.INFO, "Database backup shipped", summary, fingerprint="backup:shipped")
        await _report(bot, report_chat_id, f"✅ Backup shipped — {summary}")
        return artifact.part_count
    finally:
        # Recorded even when the upload failed part way: the parts that did land are real
        # messages sitting in the channel, and the sweep can only ever delete ids it was
        # told about. Cleanup is here for the same reason — the workdir holds a whole
        # encrypted database, and leaking one per failed run fills the container's disk.
        await _record(redis, day, chat_id, sent, settings.backup.keep_days)
        artifact.cleanup()


@broker.task(task_name="backup:prune", schedule=[{"cron": "23 4 * * *"}])
async def prune_backups() -> int:
    """Delete the backup messages this bot recorded once they pass BACKUP_KEEP_DAYS.

    The channel is never scanned, listed or pattern-matched. The only messages eligible are
    the ids `run_backup` wrote to Redis: whatever else lives in that channel belongs to
    somebody else, and a sweep that decides for itself what looks like a backup is one bad
    guess away from deleting it.

    Scheduled at 04:23 rather than on the hour so it does not land on the same tick as every
    other cron job, and comfortably after the 03:00 default upload.
    """
    container = get_container()
    settings = await container.get(Settings)
    redis = await container.get(Redis)

    index = CacheKeys.backup_days()
    try:
        members = await redis.smembers(index)
    except RedisError as exc:
        logger.error(f"backup retention sweep skipped | error: {exc}")
        return 0
    if not members:
        return 0

    cutoff = datetime.now(UTC).date() - timedelta(days=settings.backup.keep_days)
    bot = await container.get(Bot)

    deleted = 0
    for member in sorted(members):
        raw = member.decode() if isinstance(member, bytes) else str(member)
        try:
            recorded = date.fromisoformat(raw)
        except ValueError:
            logger.warning(f"backup retention index holds a non-date member, dropping it | member: {raw!r}")
            await redis.srem(index, raw)
            continue
        if recorded >= cutoff:
            continue
        deleted += await _prune_day(bot, redis, recorded)

    if deleted:
        logger.info(f"backup retention swept | messages deleted: {deleted} | cutoff: {cutoff}")
    return deleted


async def _upload(bot: Bot, chat_id: int, artifact: BackupArtifact, sent: list[int]) -> None:
    """Manifest first, then the parts in order, appending every delivered id to `sent`.

    `sent` is filled in place rather than returned so the caller still has the ids of the
    messages that did land when this raises half way through a ten-part upload.
    """
    manifest = await bot.send_message(chat_id=chat_id, text=artifact.manifest())
    sent.append(manifest.message_id)
    for index, part in enumerate(artifact.parts, start=1):
        message = await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(part),
            caption=f"part {index}/{artifact.part_count}",
            # Telegram would otherwise sniff the bytes and re-type a part as, say, a video,
            # which changes what comes back out on download. These are opaque ciphertext
            # slices and have to survive the round trip byte for byte.
            disable_content_type_detection=True,
            request_timeout=UPLOAD_TIMEOUT,
        )
        sent.append(message.message_id)


async def _prune_day(bot: Bot, redis: Redis, day: date) -> int:
    """Delete one day's recorded messages. Returns how many actually went."""
    key = CacheKeys.backup(day)
    entries = await redis.lrange(key, 0, -1)

    deleted = 0
    stuck = 0
    for raw_entry in entries:
        # Decoded rather than handed back as bytes: `lrem` matches on the encoded value, and
        # the payload is `orjson`-serialised ASCII, so str and bytes are the same bytes on
        # the wire. redis-py's own signature only admits `str` here.
        entry = raw_entry.decode() if isinstance(raw_entry, bytes) else str(raw_entry)
        record = orjson.loads(entry)
        try:
            await bot.delete_message(chat_id=record["chat_id"], message_id=record["message_id"])
        except TelegramAPIError as exc:
            # Telegram decides what a bot may delete, not us: without can_delete_messages in
            # the channel the 48-hour rule applies and nothing this old can go, and a message
            # someone already removed by hand is gone too. Log it and carry on — one message
            # the bot may not touch must not strand every later one in the sweep.
            stuck += 1
            logger.warning(
                f"backup message not deleted | day: {day} | chat_id: {record['chat_id']} | "
                f"message_id: {record['message_id']} | error: {exc}",
            )
            continue
        deleted += 1
        # Dropped from the record as it goes, so tomorrow's sweep retries only what failed
        # instead of asking Telegram to delete what is already gone.
        await redis.lrem(key, 1, entry)

    if stuck == 0:
        # Nothing left to retry — including the case where the key had already expired, which
        # is also how a stale index member gets cleaned up.
        await redis.delete(key)
        await redis.srem(CacheKeys.backup_days(), day.isoformat())
    logger.info(f"backup day pruned | day: {day} | deleted: {deleted} | undeletable: {stuck}")
    return deleted


async def _record(redis: Redis, day: date, chat_id: int, message_ids: list[int], keep_days: int) -> None:
    """Remember what went up, so the sweep has something it is allowed to delete later.

    The chat id is stored alongside each message id rather than read back from settings at
    sweep time: BACKUP_CHAT_ID can be repointed at a new channel, and messages in the old one
    would then be undeletable by a sweep that only knew about the new one.
    """
    if not message_ids:
        return
    key = CacheKeys.backup(day)
    ttl = int(timedelta(days=keep_days + RETENTION_GRACE_DAYS).total_seconds())
    payload = [orjson.dumps({"chat_id": chat_id, "message_id": message_id}) for message_id in message_ids]
    try:
        # One transaction: a key pushed without its TTL leaks forever, and a day indexed
        # without its key sends the sweep looking for ids that are not there.
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.rpush(key, *payload)
            await pipe.expire(key, ttl)
            await pipe.sadd(CacheKeys.backup_days(), day.isoformat())
            await pipe.execute()
    except RedisError as exc:
        logger.error(
            f"backup message ids not recorded | day: {day} | count: {len(message_ids)} | error: {exc} — "
            f"these messages will have to be deleted from the channel by hand",
        )


async def _fail(bot: Bot, notifier: NotifierService, report_chat_id: int | None, exc: Exception) -> None:
    """One failure, reported everywhere it is owed: the log, the notifier, the requester."""
    kind = type(exc).__name__
    reason = str(exc) or kind
    logger.error(f"backup failed | {kind}: {reason}")
    # Fingerprinted by exception type, so "no age key configured" and "pgbackup has stopped
    # producing dumps" do not share a cooldown and silence one another. They are different
    # situations needing different responses — see the NoBackupFound docstring.
    await notifier.send(AlertLevel.ERROR, "Database backup failed", f"{kind}: {reason}", fingerprint=f"backup:{kind}")
    await _report(bot, report_chat_id, f"❌ Backup failed — {kind}: {reason}")


async def _report(bot: Bot, chat_id: int | None, text: str) -> None:
    """Echo an outcome to whoever ran /backup. Never raises: the report is a courtesy.

    Escaped, because the bot's default parse mode is HTML and these strings carry exception
    messages — `age`'s stderr and the "<no stderr>" placeholder both contain angle brackets,
    and an unescaped one makes Telegram reject the whole message. The report about the
    failure would then itself fail, silently.
    """
    if chat_id is None:
        return
    try:
        await bot.send_message(chat_id=chat_id, text=html.escape(text))
    except TelegramAPIError as exc:
        logger.warning(f"backup report not delivered | chat_id: {chat_id} | error: {exc}")
