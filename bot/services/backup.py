"""Backupgram: turn the newest database dump on disk into Telegram-sized ciphertext.

This service does NOT dump the database. `pgbackup` already does that on its own
schedule into a volume this process mounts read-only (see `worker` in
docker-compose.yml); re-dumping would double the load on Postgres for nothing. What
happens here is discovery, encryption, splitting and the manifest — sending is Task 4's
job, and restoring is `scripts/postgres/decrypt`'s.

The deployment holds only the *public* half of the age keypair. It can write ciphertext
it cannot read back, which is the whole point: a compromised bot host leaks nothing but
the fact that backups exist.
"""

from __future__ import annotations
import asyncio
import contextlib
import hashlib
import html
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from asyncio import StreamReader
    from hashlib import _Hash
    from io import BufferedWriter

    from bot.core.config import BackupSettings

# Telegram caps a document sent by a *bot* at 50 MB; the 2 GB figure in the docs is for a
# self-hosted Bot API server, which this template does not assume. Parts are cut at 45 MiB
# rather than at the ceiling because the limit applies to the uploaded document as a whole:
# multipart framing, the filename and the caption all ride along and none of that is exactly
# predictable from here.
PART_BYTES = 45 * 1024 * 1024
# How much ciphertext is moved out of `age`'s pipe per hop. Bounded deliberately — this
# buffer is the only thing the pipeline holds in memory, so peak RSS is this number
# regardless of whether the dump is 50 KB or 50 GB.
READ_CHUNK_BYTES = 1024 * 1024
# Two digits minimum, so lexical order (what a shell glob, `ls`, or a Telegram channel
# export gives you) matches numeric order.
MIN_INDEX_WIDTH = 2

# What actually lands in /backups, verified against a live
# `prodrigestivill/postgres-backup-local` container rather than assumed:
#
#   /backups/daily/<db>-<YYYYMMDD>.sql.gz
#   /backups/weekly/<db>-<YYYYWW>.sql.gz
#   /backups/monthly/<db>-<YYYYMM>.sql.gz
#   /backups/last/<db>-<YYYYMMDD-HHMMSS>.sql.gz      <- the only directory that keeps history
#   /backups/{daily,weekly,monthly,last}/<db>-latest.sql.gz   <- SYMLINK, not a dump
#
# and, from `make backup` (scripts/postgres/backup), a pg_dump custom-format archive at the
# root: /backups/backup-<YYYY-MM-DD-HHMMSS>.dump.gz. Both producers are shipped; `decrypt`
# sniffs the PGDMP magic to pick pg_restore or psql, so this end does not have to care which.
DUMP_PATTERNS = ("*.sql.gz", "*.dump.gz")

# daily/, weekly/ and monthly/ are HARDLINKS to the newest file in last/ — one inode, four
# names, identical mtime down to the nanosecond — so "newest by mtime" is a four-way tie on
# every single pgbackup run. Broken towards last/, whose filename is the only one carrying
# the time of day and the only one not overwritten in place by the next run. Anything outside
# these four directories (a manual `make backup` at the root) is a different producer, not
# part of the hardlink group, and takes precedence: it was a deliberate operator action.
_DIR_RANK = {"monthly": 0, "weekly": 1, "daily": 2, "last": 3}
_UNRANKED_DIR = len(_DIR_RANK)


class BackupError(Exception):
    """Base for everything `BackupService` raises. Task 4 turns these into alerts."""


class BackupNotConfigured(BackupError):  # noqa: N818 - name is a fixed contract from the phase 12 plan
    """No age recipient is configured, so there is nothing safe to produce."""


class NoBackupFound(BackupError):  # noqa: N818 - matches BackupNotConfigured; see the phase 12 plan
    """Nothing to ship. Deliberately NOT the same error as a failure to encrypt one.

    "No backup exists yet" (a fresh deployment, ten minutes old) and "backups stopped
    three days ago" are different operational situations that need different responses,
    and collapsing them into one alert makes the second one invisible.
    """


class BackupEncryptionError(BackupError):
    """`age` did not produce a complete ciphertext."""


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":  # noqa: PLR2004 - 1024 is the unit step, not a magic threshold
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    raise AssertionError  # pragma: no cover - the loop always returns


def _write_and_hash(handle: BufferedWriter, digest: _Hash, data: memoryview) -> None:
    """Write one slice and fold it into the running digest, off the event loop.

    Both calls release the GIL for buffers this size, and they are handed to the worker
    thread *together* so a chunk costs one hop instead of two. The digest is built here,
    while the parts are written, rather than by re-reading the assembled ciphertext
    afterwards — re-reading would double the I/O and, worse, would hash a file that is no
    longer the one that was uploaded if anything touched it in between.
    """
    handle.write(data)
    digest.update(data)


def _renumber(parts: list[Path]) -> list[Path]:
    """Give every part the same zero-padded index width.

    The width is only knowable once the last part exists, so parts are written with a
    provisional index and renamed here (same directory, same filesystem — a metadata
    operation). A mixed width is exactly the trap the padding exists to remove: past 100
    parts `part99` sorts *after* `part100`, so `cat part*` assembles the ciphertext out of
    order. `scripts/postgres/decrypt` sorts numerically and would survive that, but nothing
    guarantees the operator reaches for `decrypt` rather than a shell.
    """
    width = max(MIN_INDEX_WIDTH, len(str(len(parts) - 1)))
    renamed = []
    for index, path in enumerate(parts):
        base = path.name.rsplit(".part", 1)[0]
        renamed.append(path.rename(path.with_name(f"{base}.part{index:0{width}d}")))
    return renamed


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    """One encrypted dump, split and ready to upload.

    `parts` live in `workdir`, a private temporary directory. The caller owns it: call
    `cleanup()` once the parts are uploaded, or the container's writable layer grows by a
    whole database on every run.
    """

    parts: list[Path]
    total_bytes: int
    sha256: str
    source_name: str
    source_bytes: int
    workdir: Path

    @property
    def part_count(self) -> int:
        return len(self.parts)

    def manifest(self) -> str:
        """The message that accompanies the parts. HTML — the bot's default parse mode.

        Sent for a single-part backup too. A restore path that branches on "was it split"
        is a restore path whose other branch is only ever exercised during an actual
        emergency.
        """
        return "\n".join(
            (
                "🗄 <b>Database backup</b>",
                f"source: <code>{html.escape(self.source_name)}</code>",
                f"parts: <b>{self.part_count}</b>",
                f"size: <b>{_human_bytes(self.total_bytes)}</b> ({self.total_bytes} bytes, encrypted)",
                f"sha256: <code>{self.sha256}</code>",
                "",
                "<i>age-encrypted. Download every part into one directory, then:</i>",
                f"<code>./scripts/postgres/decrypt -k backup-key.txt -s {self.sha256} ./parts/</code>",
            ),
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


class BackupService:
    """Discovery, encryption, splitting and the manifest. Stateless; safe to share."""

    def __init__(self, settings: BackupSettings) -> None:
        self._settings = settings

    async def prepare(self) -> BackupArtifact:
        """Encrypt the newest dump and cut it into uploadable parts.

        Raises `BackupNotConfigured` with no key, `NoBackupFound` with nothing to ship,
        `BackupEncryptionError` when `age` does not complete.
        """
        if not self._settings.age_public_key:
            msg = (
                "BACKUP_AGE_PUBLIC_KEY is not set. Backupgram will not upload an unencrypted "
                "database dump to Telegram. Generate a keypair with `age-keygen`, put the public "
                "key in BACKUP_AGE_PUBLIC_KEY, and keep the private key offline."
            )
            raise BackupNotConfigured(msg)

        source = self._discover()
        source_bytes = source.stat().st_size
        logger.info(f"backup source selected | file: {source} | bytes: {source_bytes}")

        workdir = Path(tempfile.mkdtemp(prefix="backupgram-"))
        try:
            parts, total_bytes, digest = await self._encrypt_and_split(source, workdir)
        except BaseException:
            # Including cancellation: a half-written part left in /tmp is indistinguishable
            # from a complete one to whoever finds it next.
            shutil.rmtree(workdir, ignore_errors=True)
            raise

        logger.info(f"backup prepared | parts: {len(parts)} | bytes: {total_bytes} | sha256: {digest}")
        return BackupArtifact(
            parts=parts,
            total_bytes=total_bytes,
            sha256=digest,
            source_name=source.name,
            source_bytes=source_bytes,
            workdir=workdir,
        )

    def _discover(self) -> Path:
        """The newest dump under the backups directory, searched recursively."""
        root = Path(self._settings.dir)
        candidates = [
            path
            for pattern in DUMP_PATTERNS
            for path in root.rglob(pattern)
            # `is_symlink` first and short-circuiting: every pgbackup directory holds a
            # `<db>-latest.sql.gz` symlink onto a file this walk has already found. Following
            # it would ship the same bytes under a name that means "whatever was newest at
            # the time", which is the one name a restore must never have to interpret.
            if not path.is_symlink() and path.is_file()
        ]
        if not candidates:
            msg = (
                f"No database dump found under {root} — searched recursively for "
                f"{', '.join(DUMP_PATTERNS)}. Either nothing has been produced yet (a fresh "
                f"deployment: pgbackup runs on SCHEDULE, not at startup) or it has stopped "
                f"producing them. Check `docker compose logs pgbackup` before assuming the "
                f"first case: this is the error that separates 'no backup yet' from 'no "
                f"backups since Tuesday'."
            )
            raise NoBackupFound(msg)
        return max(candidates, key=_recency)

    async def _encrypt_and_split(self, source: Path, workdir: Path) -> tuple[list[Path], int, str]:
        # An absolute path, resolved here rather than left to PATH: it makes a missing `age`
        # fail with "age is not installed" instead of a FileNotFoundError three frames deep.
        age = shutil.which("age")
        if age is None:
            msg = "age is not installed or not on PATH (the worker image installs it; see Dockerfile)."
            raise BackupEncryptionError(msg)

        # `create_subprocess_exec`, never a shell. The recipient comes from configuration
        # rather than from a user, but a shell here buys nothing and costs an injection
        # surface. No `-o`: age writes ciphertext to stdout, which is piped straight into the
        # splitter, so the full-size intermediate file never exists and the disk holds one
        # copy of the ciphertext instead of two.
        proc = await asyncio.create_subprocess_exec(
            age,
            "-r",
            self._settings.age_public_key,
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdout is None or proc.stderr is None:  # pragma: no cover - both were requested as PIPE
            msg = "age was started without the pipes it was asked for"
            raise BackupEncryptionError(msg)

        try:
            # stderr is drained CONCURRENTLY with stdout. Read in sequence, age blocks on a
            # full stderr pipe (64 KiB on Linux) while this side waits on stdout, and the
            # backup hangs forever instead of failing.
            split, stderr = await asyncio.gather(
                _split(proc.stdout, workdir, f"{source.name}.age"),
                proc.stderr.read(),
            )
        except BaseException:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
            raise

        returncode = await proc.wait()
        if returncode != 0:
            # stderr verbatim in the message. An `age` failure that surfaces three steps
            # later as "no such file" is an hour of somebody's day; "age: error: malformed
            # recipient" is ten seconds of it.
            detail = stderr.decode(errors="replace").strip() or "<no stderr>"
            msg = f"age exited {returncode} while encrypting {source.name} to {self._settings.age_public_key}: {detail}"
            raise BackupEncryptionError(msg)

        parts, total_bytes, digest = split
        if not parts:
            msg = f"age exited 0 but produced no ciphertext for {source.name}"
            raise BackupEncryptionError(msg)
        return parts, total_bytes, digest


def _recency(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_mtime_ns, _DIR_RANK.get(path.parent.name, _UNRANKED_DIR), path.name


async def _split(stream: StreamReader, workdir: Path, base_name: str) -> tuple[list[Path], int, str]:
    """Drain `stream` into `<base_name>.partNN` files, hashing as it goes.

    Returns the parts in order, the total ciphertext size, and the SHA-256 of the whole
    ciphertext — the digest the restore path checks after reassembly, not a per-part one.
    """
    digest = hashlib.sha256()
    parts: list[Path] = []
    total = 0
    handle: BufferedWriter | None = None
    in_part = 0
    try:
        while True:
            chunk = await stream.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            # A read can return fewer bytes than asked for, so a chunk can straddle a part
            # boundary. Sliced through a memoryview rather than re-sliced as bytes: the
            # latter copies the tail of every chunk.
            view = memoryview(chunk)
            while view:
                if handle is None:
                    # Provisional index; `_renumber` pads them all once the count is known.
                    path = workdir / f"{base_name}.part{len(parts)}"
                    handle = path.open("wb")
                    parts.append(path)
                    in_part = 0
                piece = view[: PART_BYTES - in_part]
                await asyncio.to_thread(_write_and_hash, handle, digest, piece)
                written = len(piece)
                in_part += written
                total += written
                view = view[written:]
                if in_part >= PART_BYTES:
                    handle.close()
                    handle = None
    finally:
        if handle is not None:
            handle.close()
    return _renumber(parts), total, digest.hexdigest()
