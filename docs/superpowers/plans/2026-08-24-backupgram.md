# Backupgram Implementation Plan (spec phase 12)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship encrypted database backups to a private Telegram channel, so a deployment that loses its volume can still be restored — without the bot ever being able to read what it uploaded.

**Architecture:** The existing `pgbackup` service already writes compressed dumps to the `backups-data` volume every 30 minutes with day/week/month retention. Backupgram mounts that volume **read-only**, encrypts the newest artifact with `age` to an X25519 recipient, splits it under Telegram's 50 MB document ceiling, and uploads it behind a manifest. It does not dump the database.

**Tech Stack:** `age` 1.2.1 (Alpine community package), TaskIQ, Redis, aiogram 3.30.

**Spec:** `docs/superpowers/specs/2026-08-23-scalable-architecture-design.md` — section 18.

**Depends on:** the notifier (spec phase 11, `docs/superpowers/plans/2026-08-24-notifier.md`) — both surfaces report through it. That plan must land first.

---

## The two properties that define this feature

**1. It does not dump the database.** `pgbackup` (`prodrigestivill/postgres-backup-local`) already
does that on a `@every 0h30m00s` schedule into the `backups-data` volume. Re-dumping would double
the load on Postgres for no benefit and produce a second, inconsistent set of artifacts.
Backupgram mounts that volume read-only and ships what is already there.

**2. The bot cannot decrypt its own backups.** Encryption is `age -r $BACKUP_AGE_PUBLIC_KEY` — an
X25519 *public* key. The deployment holds only that. An attacker who compromises the bot, the
container, or the Telegram account gets ciphertext and nothing else. The private key never enters
the deployment; the operator keeps it offline and uses it only when restoring.

That property is the whole point, and it has a sharp edge the README must state plainly: **lose
the private key and every backup is unrecoverable.** Tell operators to escrow it before enabling
the feature.

**There is no plaintext path and no override flag.** If `BACKUP_AGE_PUBLIC_KEY` is unset the task
refuses to run and raises a notifier alert. This is a template others copy verbatim, and an
escape hatch becomes somebody's production configuration.

## Global Constraints

- Python floor **3.14**; all tooling through `uv`. `uv sync` after any `pyproject.toml` change.
- Logging is `from loguru import logger`; stdlib `logging` is forbidden.
- ruff `select = ["ALL"]`, `fix = true`. **`uv run ruff check .` mutates files** — run it before
  reading your diff. mypy strict, 0 errors.
- **No tests.** Do not create `tests/`. Verify with throwaway scripts and live runs.
- Conventional commits; **never** a `Co-Authored-By` trailer.
- **Runtime annotation hazard** — dishka, aiogram, FastAPI and SQLAlchemy resolve annotations at
  RUNTIME. Types in handler signatures, dishka providers and `Mapped[...]` must be module-level
  imports, never under `if TYPE_CHECKING:`; use a targeted `# noqa: TC00x` where ruff disagrees.
- **No new Python dependency.** `age` is a system binary added to the image with `apk`.
- The `api` container cannot start with a fake `BOT_TOKEN` (`lifespan` calls `get_me()`), so drive
  the app in-process with a stubbed `AiohttpSession.make_request` rather than running it.

## File Structure

| File | Responsibility |
|---|---|
| `bot/services/backup.py` | discovery, encryption, splitting, manifest construction |
| `bot/tasks/backup.py` | the scheduled job and the retention sweep |
| `bot/handlers/admin_backup.py` | `/backup` admin command |
| `scripts/postgres/backup` | **fix**: stop hardcoding the database name |
| `scripts/postgres/decrypt` | **new**: the restore path |
| `Dockerfile` | `apk add --no-cache age` |
| `docker-compose.yml` | mount `backups-data` read-only into `worker` |

---

## Task 1: Settings, the `age` binary, and the read-only mount

**Files:**
- Modify: `bot/core/config.py`, `.env.example`, `Dockerfile`, `docker-compose.yml`

**Interfaces:**
- Produces: `settings.backup` with `age_public_key: str`, `chat_id: int | None`, `schedule: str`, `dir: str = "/backups"`, `keep_days: int`; `age` on `PATH` in the image; `backups-data` mounted at `/backups:ro` in the `worker` service.

- [ ] **Step 1: Add `BackupSettings`**

Follow the nested style of `WebAppSettings` / `PaymentSettings` / `NotifierSettings`:

```python
class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="BACKUP_")

    age_public_key: str = ""
    chat_id: int | None = None
    schedule: str = "0 3 * * *"      # daily at 03:00
    dir: str = "/backups"
    keep_days: int = 30
```

`age_public_key` defaults to empty and that empty value is a **refusal**, not a fallback — see
Task 3. Do not give it a default key; a shipped keypair would mean every deployment of this
template encrypts to the same recipient, which is the same as not encrypting.

Add all five to `.env.example`. On `BACKUP_AGE_PUBLIC_KEY`, state in the comment: generate with
`age-keygen`, put **only** the public key here, and **escrow the private key offline — without it
the backups cannot be read by anyone, including you.**

- [ ] **Step 2: Put `age` in the image**

`Dockerfile` is `ghcr.io/astral-sh/uv:0.12-python3.14-alpine`. Add before the `uv sync` layer:

```dockerfile
RUN apk add --no-cache age
```

Verify the package exists and note the version you got:

```bash
docker compose build worker
docker compose run --rm --no-deps --entrypoint sh worker -c "age --version && which age"
```

The spec says 1.2.1 is in Alpine community. If `apk` cannot find it, **stop and report** — the
alternatives (downloading a release binary, building from source) change the image's trust story
and are not yours to choose unilaterally.

- [ ] **Step 3: Mount the backups volume read-only**

The `pgbackup` service writes to the `backups-data` volume. Give `worker` the same volume, read
only:

```yaml
    volumes:
      - backups-data:/backups:ro
```

**`:ro` is load-bearing.** Backupgram reads artifacts another service owns; a writable mount
would let a bug in this feature corrupt or delete the actual backups it exists to protect.

Only `worker` needs it — the scheduled task and the `/backup` command both execute there, not in
`api`.

- [ ] **Step 4: Verify the mount and the binary together**

```bash
docker compose up -d --wait postgres pgbouncer redis worker
docker compose exec worker sh -c "age --version; ls -la /backups | head"
docker compose exec worker sh -c "touch /backups/canary 2>&1 || echo '  write refused (correct)'"
```

Expected: `age` reports a version, `/backups` lists whatever `pgbackup` has written, and the
write is refused. That third line is the one worth having — a mount that is writable when you
believed it read-only fails silently until something destroys a backup.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/core/config.py .env.example Dockerfile docker-compose.yml
git commit -m "feat(backup): age binary, backup settings and a read-only backups mount"
```

---

## Task 2: Fix the broken backup script and add the restore path

**Files:**
- Modify: `scripts/postgres/backup`, `Makefile`
- Create: `scripts/postgres/decrypt`

**Interfaces:**
- Produces: a `backup` script that works against a real deployment; `scripts/postgres/decrypt` for the restore path.

**This is a pre-existing bug, verified:** `scripts/postgres/backup` runs

```sh
pg_dump -Fc app -U "$POSTGRES_USER" | gzip > "$backup_filename"
```

hardcoding the database name `app`, while compose provisions `${DB_NAME}`. It therefore fails
against any real deployment. `make backup` is broken twice over — it runs
`docker compose exec api scripts/postgres/backup`, and `api` is the Python image, which has no
`pg_dump`. `make restore` names a service `app_db` that does not exist.

- [ ] **Step 1: Fix the script**

Replace the hardcoded `app` with `"$POSTGRES_DB"`, and fail loudly when it is unset rather than
dumping something unintended:

```sh
: "${POSTGRES_DB:?POSTGRES_DB is not set}"
: "${POSTGRES_USER:?POSTGRES_USER is not set}"
pg_dump -Fc "$POSTGRES_DB" -U "$POSTGRES_USER" | gzip > "$backup_filename"
```

`-e` is already set on the shebang line, so a `pg_dump` failure aborts — but note that in a
pipeline only the *last* command's status is seen without `pipefail`, so a failing `pg_dump`
currently still writes a truncated `.gz` and reports success. Add `set -o pipefail` if the shell
supports it, or check `${PIPESTATUS[0]}`; say in your report which you used and why.

- [ ] **Step 2: Fix the Makefile targets**

`make backup` must run in a container that actually has `pg_dump` — that is `postgres`, not
`api`. `make restore` must name a service that exists. Correct both, and point them at the
`postgres` service.

- [ ] **Step 3: Write `scripts/postgres/decrypt`**

The restore path for what Backupgram uploads. It takes the downloaded part files and the private
key, and it must:

1. Concatenate `backup-<ts>.dump.gz.age.partNN` in numeric order — `cat part*` in shell glob
   order is wrong once there are more than nine parts, since `part10` sorts before `part2`.
2. Verify the SHA-256 of the assembled ciphertext against the digest from the manifest, and
   **stop** on mismatch. Restoring a truncated dump over a live database is worse than not
   restoring.
3. `age -d -i <private-key-file>` and pipe into `gunzip | pg_restore`.

Print what it is about to do and require an explicit confirmation before touching the target
database. Take the private key path as an argument; never read it from an environment variable
that might be logged.

- [ ] **Step 4: Verify the round trip end to end**

This is the only step that proves the feature is worth having. On a private Postgres:

```bash
# make a dump, encrypt it, split it, reassemble it, decrypt it, restore it, compare
```

Show: a table with known rows → dump → `age -r <pub>` → split into ≥2 parts → reassemble →
digest matches → `age -d -i <priv>` → `pg_restore` into a *different* empty database → the rows
are identical.

Generate a throwaway keypair with `age-keygen` for this. Assert the digest check **fails** on a
deliberately corrupted part, too — a verification that only ever passes proves nothing about the
guard.

- [ ] **Step 5: Lint and commit**

```bash
git add scripts/postgres/ Makefile
git commit -m "fix(backup): stop hardcoding the database name, and add a restore path"
```

---

## Task 3: The backup service — discovery, encryption, splitting, manifest

**Files:**
- Create: `bot/services/backup.py`
- Modify: `bot/core/di.py`

**Interfaces:**
- Consumes: `settings.backup` (Task 1); `age` on `PATH`.
- Produces: `BackupService.prepare() -> BackupArtifact` where `BackupArtifact` carries `parts: list[Path]`, `total_bytes: int`, `sha256: str`, `source_name: str`; raises `BackupNotConfigured` when the key is missing.

- [ ] **Step 1: Refuse without a key, loudly**

The first thing `prepare()` does:

```python
if not self._settings.age_public_key:
    msg = (
        "BACKUP_AGE_PUBLIC_KEY is not set. Backupgram will not upload an unencrypted "
        "database dump to Telegram. Generate a keypair with `age-keygen`, put the public "
        "key in BACKUP_AGE_PUBLIC_KEY, and keep the private key offline."
    )
    raise BackupNotConfigured(msg)
```

No flag disables this. Task 4 turns the exception into a notifier alert.

- [ ] **Step 2: Find the newest artifact**

`pgbackup` writes into `/backups` with its own daily/weekly/monthly layout. Find the most recent
regular file matching its dump pattern, searching recursively — do not assume a flat directory,
and do not assume a filename format you have not looked at. **Run `ls -R /backups` in the
container first and write the glob against what is actually there**, then say in your report what
the layout was.

If no artifact is found, raise a distinct error. "No backup exists yet" and "backups are broken"
are different operational situations and must not produce the same alert.

- [ ] **Step 3: Encrypt with `age`, streaming**

```
age -r <public-key> -o <out>.age <in>
```

Run it with `asyncio.create_subprocess_exec`, never `shell=True` — the key comes from
configuration, but a shell here is an injection surface for no benefit. Check the return code and
include `stderr` in the exception message; an `age` failure that surfaces as "file not found"
three steps later costs an hour.

`age` streams, so memory stays flat regardless of dump size. Do **not** read the dump into
Python.

- [ ] **Step 4: Split under Telegram's ceiling, and build the manifest**

Telegram caps bot document uploads at **50 MB** (2 GB only via a self-hosted Bot API server).
Split the *ciphertext* into `backup-<ts>.dump.gz.age.partNN` with a zero-padded, two-digit
minimum index so lexical order matches numeric order.

Use a chunk size below the cap with headroom — 45 MB — because the limit applies to the uploaded
document and you do not control multipart overhead exactly.

The manifest is a message, not a file: part count, total size, and the **SHA-256 of the assembled
ciphertext** (not of each part — the restore path verifies the whole). Compute the digest while
writing the parts rather than re-reading the file.

A single-part backup still gets a manifest. A restore path that has to branch on "was it split"
is a restore path that gets tested only in the common case.

- [ ] **Step 5: Verify with a file that genuinely needs splitting**

Create a >100 MB source so the split path really runs — a small file would exercise only the
single-part branch. Show: part count ≥ 3, every part ≤ 45 MB, `partNN` naming zero-padded, the
manifest digest equal to `sha256sum` of the concatenation, and `age -d` of the reassembled
ciphertext byte-identical to the source.

Also assert **peak RSS stays flat** while encrypting that 100 MB file — that is what "it streams"
means, and it is the claim most likely to be quietly false.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/services/backup.py bot/core/di.py
git commit -m "feat(backup): discover, encrypt, split and manifest the newest dump"
```

---

## Task 4: The scheduled task, retention, and the `/backup` command

**Files:**
- Create: `bot/tasks/backup.py`, `bot/handlers/admin_backup.py`
- Modify: `bot/tasks/__init__.py`, `bot/handlers/__init__.py`, `bot/cache/keys.py`

**Interfaces:**
- Consumes: `BackupService` (Task 3), `NotifierService` (phase 11).
- Produces: scheduled task `backup:run`; retention sweep `backup:prune`; `/backup` admin command.

- [ ] **Step 1: The scheduled task**

```python
@broker.task(task_name="backup:run", schedule=[{"cron": <settings.backup.schedule>}])
async def run_backup() -> int:
```

A cron expression from configuration cannot go in a decorator literal — the decorator is
evaluated at import. Read how `bot/tasks/__init__.py` builds its `LabelScheduleSource` and pick
the mechanism that lets the schedule come from settings; if the only honest option is a fixed
label, say so in your report rather than pretending `BACKUP_SCHEDULE` works.

Behaviour: send the manifest message first, then each part in order as a document, recording
every `message_id`. Report success or failure through the notifier — `INFO` with the part count
and total size on success, `ERROR` with the reason on failure, including `BackupNotConfigured`.

Store the message ids in Redis under a `CacheKeys.backup(date)` key with a TTL longer than
`keep_days`, so the retention sweep can find them.

- [ ] **Step 2: Retention**

```python
@broker.task(task_name="backup:prune", schedule=[{"cron": "23 4 * * *"}])
async def prune_backups() -> int:
```

Delete this bot's own backup messages older than `BACKUP_KEEP_DAYS`, using the ids tracked in
Redis. Telegram only lets a bot delete its own messages and only within its own limits — catch
`TelegramAPIError` per message, log, and continue. One undeletable message must not stop the
sweep.

Never delete by scanning the channel. Only ids this bot recorded are eligible; anything else in
that channel belongs to someone else.

- [ ] **Step 3: The `/backup` command**

`bot/handlers/admin_backup.py`, modelled on the existing `bot/handlers/admin_payments.py` —
`Command("backup")` plus `AdminFilter()`, with the `# ruff: noqa: TC001, TC002` header those
runtime-resolved signatures need.

It must **enqueue** `backup:run` and reply immediately, not run the backup inline. Encrypting and
uploading a multi-part archive takes far longer than Telegram's handler window, and doing it in
the handler blocks the update pipeline.

Reply with something honest — "backup started, you'll get a report in this chat" — and register
the router in `get_handlers_router()`.

- [ ] **Step 4: Verify all three surfaces**

Private containers, `AiohttpSession.make_request` stubbed to capture outbound methods. Show:
- `run_backup` with a key configured: one `SendMessage` manifest followed by N `SendDocument`
  calls in part order, and a notifier `INFO`;
- `run_backup` with `BACKUP_AGE_PUBLIC_KEY` unset: **zero** `SendDocument` calls and a notifier
  `ERROR`. This is the most important row in the table — a bug here uploads a plaintext database
  dump to Telegram;
- `prune_backups`: deletes only ids older than `keep_days`, leaves newer ones, and survives a
  `TelegramForbiddenError` on one message by continuing to the rest;
- `/backup` from an admin: enqueues and replies without blocking; from a non-admin: no reply and
  no enqueue.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/ 
git commit -m "feat(backup): scheduled encrypted backups, retention and an admin command"
```

---

## Task 5: Document it

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: The README section**

Under the existing emoji-prefixed `##` headings, add a Backupgram section covering:

- What it does and — explicitly — what it does **not**: it ships the dumps `pgbackup` already
  makes, it does not dump the database itself.
- `age-keygen` to generate a keypair; only the public key goes in `.env`.
- **Escrow the private key offline before enabling this.** Losing it makes every backup
  unrecoverable, by design. State this as its own line, not inside a paragraph.
- The bot cannot decrypt what it uploads — that is the point, and it is what makes putting
  backups in a chat acceptable.
- Telegram's 50 MB per-document limit and what the parts/manifest look like.
- The restore procedure, as literal commands, ending with `scripts/postgres/decrypt`.

- [ ] **Step 2: Follow your own restore instructions literally**

Take the README's restore section and run it, exactly as written, against a database you are
willing to destroy. If a step is missing or in the wrong order, the instructions are wrong —
fix them, do not fix your run. Paste the transcript.

This is the same standard the admin plan's migration message was held to: documentation that
tells an operator what to do during an emergency is only as good as the last time somebody
followed it verbatim.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document Backupgram, key escrow and the restore procedure"
```

---

## Verification checklist for the whole plan

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean; `git diff` empty afterwards
- [ ] No `tests/` directory; no new Python dependency
- [ ] `age --version` works inside the built image
- [ ] `/backups` is mounted read-only in `worker` — a write is refused
- [ ] With `BACKUP_AGE_PUBLIC_KEY` unset, **no document is uploaded** and an alert is raised
- [ ] A >100 MB dump splits into parts ≤ 45 MB, zero-padded, and reassembles to a matching digest
- [ ] A corrupted part makes the digest check fail
- [ ] `age -d` with the private key reproduces the original dump byte-for-byte
- [ ] `scripts/postgres/backup` uses `$POSTGRES_DB`, not `app`
- [ ] The README's restore procedure has been followed verbatim and works

## Out of scope

- Uploading to S3, GCS or any non-Telegram destination.
- A self-hosted Bot API server to lift the 50 MB limit. The split path handles it.
- Point-in-time recovery or WAL archiving; this ships `pg_dump` artifacts.
- Encrypting the backups `pgbackup` writes to the volume. They are protected by the volume, not
  by Backupgram; what leaves the host is what gets encrypted.
