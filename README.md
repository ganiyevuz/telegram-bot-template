<h1 align="center"><em>Telegram bot template</em></h1>

<h3 align="center">
  Best way to create a scalable telegram bot with analytics
</h3>

<p align="center">
  <a href="https://github.com/donbarbos/telegram-bot-template/tags"><img alt="GitHub tag (latest SemVer)" src="https://img.shields.io/github/v/tag/donbarbos/telegram-bot-template"></a>
  <a href="https://github.com/donbarbos/telegram-bot-template/actions/workflows/linters.yml"><img src="https://img.shields.io/github/actions/workflow/status/donbarbos/telegram-bot-template/linters.yml?label=linters" alt="Linters Status"></a>
  <a href="https://github.com/donbarbos/telegram-bot-template/actions/workflows/docker-image.yml"><img src="https://img.shields.io/github/actions/workflow/status/donbarbos/telegram-bot-template/docker-image.yml?label=docker%20image" alt="Docker Build Status"></a>
  <a href="https://www.python.org/downloads"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python"></a>
  <a href="https://github.com/donbarbos/telegram-bot-template/blob/main/LICENSE"><img src="https://img.shields.io/github/license/donbarbos/telegram-bot-template?color=blue" alt="License"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Code style"></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="Package manager"></a>
<p>

## ✨ Features

-   [x] Admin Panel based on [`SQLAdmin`](https://aminalaee.dev/sqladmin/), mounted on the same FastAPI app at `/admin`
-   [x] Product Analytics System: using [`Amplitude`](https://amplitude.com/) or [`Posthog`](https://posthog.com/) or [`Google Analytics`](https://analytics.google.com)
-   [x] Performance Monitoring System: using [`Prometheus`](https://prometheus.io/) and [`Grafana`](https://grafana.com/)
-   [x] Tracking System: using [`Sentry`](https://sentry.io/)
-   [x] Encrypted database backups shipped to a private Telegram channel using [`age`](https://github.com/FiloSottile/age)
-   [x] Seamless use of `Docker` and `Docker Compose`
-   [x] Export all users in `.csv` (from the bot's `/export_users` command or the admin panel)
-   [x] Configured CI pipeline from git hooks to github actions
-   [x] [`SQLAlchemy V2`](https://pypi.org/project/SQLAlchemy/) is used to communicate with the database
-   [x] Database Migrations with [`Alembic`](https://pypi.org/project/alembic/)
-   [x] Ability to cache using decorator
-   [x] Convenient validation using [`Pydantic V2`](https://pypi.org/project/pydantic/)
-   [x] Internationalization (i18n) using GNU gettex and [`Babel`](https://pypi.org/project/Babel/)

## 🚀 How to Use

### 🐳 Running in Docker _(recommended method)_

The compose stack runs the **API** entrypoint (`bot.entrypoints.api`) — the `api` service is
`uvicorn` serving `/webhook`, the health probes `/health/live` and `/health/ready`, Prometheus
`/metrics`, the Mini App at `/webapp` + `/api/webapp/*`, and the admin panel at `/admin`. There
is no second application: the panel is SQLAdmin bound to the same async engine the bot already
runs on.

Alongside it, two TaskIQ services run the same `bot:latest` image with a different command:

| Service     | Command                                            | Scaling                            |
| ----------- | -------------------------------------------------- | ---------------------------------- |
| `api`       | `uvicorn bot.entrypoints.api:app` (the image `CMD`) | scales horizontally                |
| `worker`    | `taskiq worker bot.entrypoints.worker:broker`       | scales horizontally                |
| `scheduler` | `taskiq scheduler bot.entrypoints.scheduler:scheduler` | **exactly one instance, always** |

Without `worker` and `scheduler` every background task — `payments:reconcile`,
`payments:expire_premium`, `analytics:flush`, `broadcast:start`, `broadcast:chunk`,
`export:users` — is registered on a broker nothing consumes, so premium never expires,
analytics events pile up in Redis until they are trimmed away, and an admin broadcast enqueues
work that is never delivered.

`api` and `worker` are safe to scale: the broker is a Redis list, so workers *compete* for jobs
rather than each running a copy of them.

```bash
docker compose up -d --scale worker=3
```

**`scheduler` must run exactly one instance — never scale it.** It is the process that reads the
`schedule=[{"cron": ...}]` labels off the tasks and enqueues them at each tick, and it holds no
lock. A second replica reads the same labels and enqueues its own copy of every job, so each
cron fires twice. `payments:expire_premium` is idempotent and `payments:reconcile` is read-only,
so today the visible damage would be duplicate work and duplicate alerts — but `broadcast:start`
is neither, and a second scheduler would fan the same broadcast out to every user twice.

**pgbouncer runs in transaction pooling mode** (`POOL_MODE=transaction`), which is what makes
that scaling safe. A server connection is held only for the duration of a transaction, so three
`worker` replicas holding 15 client connections each do not turn into 45 Postgres backends:
`DEFAULT_POOL_SIZE=20` caps the pool and `MAX_DB_CONNECTIONS=50` is a real global ceiling
(not `0`, which means unlimited), both below Postgres's own `max_connections` — 100 by default —
with headroom for the migrator and a `psql` session. Measured on this stack under a saturating
load: one replica held 15 backends and three replicas held 20; the same load through
`POOL_MODE=session` held 40.

That mode is one half of a pair, and neither half survives being "cleaned up" on its own. Because
a server connection moves between clients between transactions, server-side prepared statements
cannot be reused, so the engine in `bot/core/di.py` is built with
`connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0}`. Re-enable either
cache and asyncpg starts raising `DuplicatePreparedStatementError` / `InvalidSQLStatementNameError`
under concurrency; move pgbouncer back to `session` and the Postgres connection count multiplies
by replica count again, which is the thing pgbouncer is in the stack to prevent.

-   configure environment variables in `.env` file

    Port variables are **host** bindings only — the container ports are pinned in
    `docker-compose.yml`. Two exceptions: `DB_PORT` and `REDIS_PORT` are also what the app dials
    inside the compose network (`pgbouncer:5432`, `redis:6379`), so leave them at `5432`/`6379`
    and change the left-hand side of the mapping if a host port is taken.

-   start services

    ```bash
    docker compose up -d --build
    ```

-   check it is up

    ```bash
    curl localhost:8080/health/ready   # 200 once Postgres and Redis are reachable
    open  localhost:8080/admin         # the panel; log in with DEFAULT_ADMIN_EMAIL/PASSWORD
    make logs-scheduler                # should show it sending analytics:flush every minute
    make logs-worker                   # should show the same task being executed
    ```

### 💻 Running on Local Machine

-   set environment and install dependencies using [uv](https://docs.astral.sh/uv/ "python package manager") (you can find branch with Poetry [here](https://github.com/donbarbos/telegram-bot-template/tree/poetry-archive))

    ```bash
    uv sync --frozen --all-groups
    ```

-   start the necessary services (at least your database and redis)

-   configure environment variables in `.env` file

-   start telegram bot with long polling (**development only** — this path serves no HTTP at
    all: no `/metrics`, no `/health/*`, no Mini App. Use the Docker stack, or run
    `uvicorn bot.entrypoints.api:app`, for anything else)

    ```bash
    make run-polling   # == uv run python -m bot
    ```

-   start the API — this is what serves the admin panel, at `/admin`

    ```bash
    uv run uvicorn bot.entrypoints.api:app --port 8080
    ```

    `ADMIN_SECRET_KEY` has no default and the process refuses to start without one while
    `ADMIN_ENABLED=True`. Generate one with
    `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

-   make migrations

    ```bash
    uv run alembic upgrade head
    ```

## 📱 Mini App

The bot ships a **demo** Mini App at `bot/webapp/static/index.html`, served by the webhook
entrypoint at `GET /webapp` and backed by `GET /api/webapp/me` and `POST /api/webapp/invoice`
(both authenticated by the `initData` signature). It is a single dependency-free page with no
build step, npm or framework — it exists to prove the launch → authenticate → pay loop works end
to end, and is meant to be **replaced** with your own front end.

Point `WEBAPP_URL` at the page and register the same URL with [@BotFather](https://t.me/BotFather)
(`/newapp`). Two things about that URL:

-   Telegram **will not load a Mini App over plain HTTP** — the URL must be HTTPS.
-   `localhost` resolves to the phone itself on mobile clients, so it never reaches your machine.
    Expose the port through a tunnel (`cloudflared`, `ngrok`, ...) for local testing.

With `WEBAPP_URL` unset the launch button is simply omitted from the main menu — Telegram rejects
a `web_app` button with an empty URL and drops the whole keyboard with it.

Running `bot.entrypoints.api` for the Mini App while keeping polling for updates is a supported
combination: it serves `/webapp` and `/api/webapp/*` either way, and mounts `POST /webhook` **only**
when `USE_WEBHOOK=True`. In polling mode that path is a 404 by design — the route has no purpose
there, and one that does not exist cannot be left unauthenticated by an empty `WEBHOOK_SECRET`.

## 🗄 Encrypted backups to Telegram

Backupgram ships the database dumps that already exist on disk to a private Telegram channel,
encrypted with [`age`](https://github.com/FiloSottile/age) so that only the holder of the
private key can read them.

**It does not dump the database.** `pgbackup` (`prodrigestivill/postgres-backup-local`) does
that every 30 minutes into the `backups-data` volume, and `worker` mounts that volume
**read-only** at `/backups`. Backupgram takes the newest file it finds there, encrypts it,
splits it and uploads it — it cannot write, rotate or delete a dump even if it tries.

| Step                         | Who does it                                                            |
| ---------------------------- | ---------------------------------------------------------------------- |
| Produce the dumps            | `pgbackup`, on its own `SCHEDULE` (every 30 min), into `backups-data`   |
| Encrypt, split and upload    | `backup:run` on `BACKUP_SCHEDULE` (`0 3 * * *`), in `worker`            |
| Delete aged-out messages     | `backup:prune`, daily at 04:23, past `BACKUP_KEEP_DAYS`                 |
| Restore                      | `scripts/postgres/decrypt`, on **your** machine — never in the deployment |

An admin can also trigger a run from the bot with `/backup`; it enqueues the same task and
reports the outcome back into that chat.

### Setting it up

-   generate a keypair **off the deployment machine**

    ```bash
    age-keygen -o backup-key.txt
    # Public key: age1m23xw7rg3h79mxegz7utngtu2u8m0apdtarek7ptx2algustxd3qphammv
    chmod 600 backup-key.txt
    ```

-   put **only the public key** in `.env` on the server, with the channel to ship to

    ```dotenv
    BACKUP_AGE_PUBLIC_KEY=age1m23xw7rg3h79mxegz7utngtu2u8m0apdtarek7ptx2algustxd3qphammv
    BACKUP_CHAT_ID=-1001234567890
    ```

    Make the channel **private**, add the bot as an administrator, and grant it *Delete
    messages* as well — without that permission the retention sweep can only ever delete
    messages under 48 hours old, which is Telegram's rule and not something this bot can
    work around.

-   **escrow the age private key offline before enabling this feature — losing it makes every backup unrecoverable, by design**

    There is no recovery path, no second copy and no override. Put `backup-key.txt` in a
    password manager, print it into a safe, hand a copy to a second person — but do not leave
    it only on the laptop that generated it, and do not put it on the server.

-   run a restore drill now, while nothing is on fire (see [Restoring a backup](#restoring-a-backup))

    A backup nobody has ever restored is a hypothesis.

`BACKUP_AGE_PUBLIC_KEY` unset is a **refusal, not a fallback**: the task raises
`BackupNotConfigured` before it reads a single byte, uploads nothing and raises an alert
through the notifier. There is no plaintext path and no override — an unencrypted dump of the
whole database must never be what lands in a chat. `BACKUP_CHAT_ID` unset is the feature's off
switch and stays silent.

### The bot cannot read back what it uploads

The deployment holds only the public half of the keypair. `age -r <public key>` encrypts;
decrypting needs the private half, which never enters the server, the image or `.env`. That
asymmetry is what makes keeping backups in a chat acceptable at all: whoever takes the bot
token, the container, the `.env` file or the Telegram account itself gets ciphertext and the
knowledge that backups exist. It applies to everyone else with access to the channel too —
being added to it is not being able to read it.

The direct cost of that property is the line above: nothing in the system can help you if the
private key is gone.

### Parts and the manifest

A bot may upload at most **50 MB per document** — the 2 GB figure in Telegram's documentation
is for a self-hosted Bot API server, which this template does not assume. Ciphertext larger
than that is cut into **45 MiB** parts, leaving headroom for the multipart framing, filename
and caption that ride along with the upload:

```
tgdb-20260824-075537.sql.gz.age.part00    47185920 bytes
tgdb-20260824-075537.sql.gz.age.part01    47185920 bytes
tgdb-20260824-075537.sql.gz.age.part02    23457177 bytes
```

Indices are zero-padded and start at `part00`, so lexical order — what a shell glob, `ls` or a
Telegram channel export hands you — is numeric order.

Every run posts a manifest message first, then the parts in order:

```
🗄 Database backup
source: tgdb-20260824-075537.sql.gz
parts: 3
size: 112.4 MiB (117829017 bytes, encrypted)
sha256: 30aa51b86c0c2fcb24c71890617335674deee23ddd11ab9baa6405d230962fbe

age-encrypted. Download every part into one directory, then:
./scripts/postgres/decrypt -k backup-key.txt -s 30aa51b86c0c2fcb24c71890617335674deee23ddd11ab9baa6405d230962fbe ./parts/
```

That `sha256` covers the **whole** encrypted archive, not any single part. It is what proves —
before anything touches a database — that you downloaded every part and that they reassembled
into exactly the bytes the worker uploaded. Keep the manifest: without the digest `decrypt`
refuses to run at all.

### Restoring a backup

The dumps `pgbackup` writes are **plain SQL** (`.sql.gz`) and carry no `DROP` statements, so
they restore into an *empty* database. `make restore` is not the tool for them: it pipes into
`pg_restore`, which only reads the custom-format `.dump.gz` that `make backup` writes.
`scripts/postgres/decrypt` detects which of the two it is holding (the custom format starts
with the literal `PGDMP`) and runs `psql` or `pg_restore` to match.

`pgbackup` is pinned to the **same major version as `postgres:`** in `docker-compose.yml`, and
it has to stay that way. `prodrigestivill/postgres-backup-local:latest` ships pg_dump 18,
whose plain-SQL output opens with `SET transaction_timeout = 0;` — a parameter a Postgres 14
server does not recognise, so the restore stops on line 13 with every table still missing. The
dumps get written and shipped exactly the same either way; only the restore fails, and only on
the day you need it.

On the machine that holds the private key you need `age`, the PostgreSQL client tools and a
checkout of this repository:

```bash
brew install age libpq && brew link --force libpq   # macOS (libpq is keg-only, hence the link)
apk add age postgresql-client                       # Alpine
apt install age postgresql-client                   # Debian/Ubuntu
```

-   collect the parts — every part of **one** backup, into an empty directory, and nothing else

    ```bash
    mkdir -p ./parts
    # download partNN of that backup from the channel into ./parts/
    ls ./parts
    ```

-   lock the key down; `decrypt` warns when anyone but you can read it

    ```bash
    chmod 600 backup-key.txt
    ```

-   give libpq the password **before** anything else, because every command below is a libpq
    client and none of them take one on the command line

    ```bash
    export PGPASSWORD='<DB_PASS>'   # or put the credentials in ~/.pgpass
    ```

    Skipping this is not a prompt you can answer later: with no terminal attached — a script,
    a CI job, `docker exec` without `-t` — `createdb` reprints `Password:` until you kill it.

-   create an **empty** database to restore into — never restore over the live one, which is
    still the only copy you have if this dump turns out to be older or thinner than you hoped

    ```bash
    createdb -h <host> -p <port> -U <user> restore_check
    psql -h <host> -p <port> -U <user> -d restore_check -c 'DROP SCHEMA public CASCADE;'
    ```

    That second line is not optional. `pgbackup` dumps with `--schema=public`, so the dump
    **creates** the schema, while `createdb` has already made one — without the drop the
    restore stops on `ERROR: schema "public" already exists` before a single table is written.
    On a database you created one command ago there is nothing in that schema to lose.

-   verify, decrypt and restore: the command from the manifest, plus that target

    ```bash
    ./scripts/postgres/decrypt \
        -k backup-key.txt \
        -s <sha256-from-the-manifest> \
        -H <host> -p <port> -U <user> -d restore_check \
        ./parts/
    ```

    It reassembles the parts in numeric order, fails on a gap in the numbering, checks the
    SHA-256 **before** decrypting and refuses on a mismatch without touching the database, then
    asks you to type the target database name back before it writes anything. In a script pass
    `-y`: with no terminal attached and no `-y` it refuses outright rather than restore
    unattended. `-c` lets `pg_restore` drop existing objects first — custom-format archives
    only; it does nothing to a plain-SQL dump.

-   check what you got, then swap it in

    ```bash
    psql -h <host> -p <port> -U <user> -d restore_check -c '\dt'
    psql -h <host> -p <port> -U <user> -d restore_check -c 'SELECT count(*) FROM users;'
    ```

To only get the file back, touching no database at all — the restore drill worth running the
day you enable this — use `-o`:

```bash
./scripts/postgres/decrypt -k backup-key.txt -s <sha256-from-the-manifest> -o restored.sql.gz ./parts/
```

#### Restoring on the deployment host

The compose stack publishes **pgbouncer** on `DB_PORT`, not Postgres, and a transaction pooler
is the wrong thing to push a whole dump through — `createdb` and `dropdb` do not work through
it at all. Decrypt to a file and restore inside the `postgres` container:

```bash
./scripts/postgres/decrypt -k backup-key.txt -s <sha256-from-the-manifest> -o restored.sql.gz ./parts/
# /tmp, not /backups: a .sql.gz under /backups is a dump as far as the next backup run is concerned
docker compose cp restored.sql.gz postgres:/tmp/restored.sql.gz
docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" restore_check'
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d restore_check -c "DROP SCHEMA public CASCADE;"'
docker compose exec postgres sh -c 'gunzip -c /tmp/restored.sql.gz | psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d restore_check'
```

Decrypting on your own machine and copying `restored.sql.gz` over keeps the private key off
the server; the plaintext dump is the whole database either way, so delete it when you are
done.

### Retention

`backup:prune` runs daily at 04:23 and deletes backup messages older than `BACKUP_KEEP_DAYS`
(30 by default). It only ever deletes **message ids this bot recorded in Redis when it
uploaded them** — the channel is never scanned, listed or pattern-matched, so anything else
kept in there is left alone, and a message the bot is not allowed to delete is logged and
skipped rather than retried forever.

## 🌍 Environment variables

to launch the bot you only need a token bot, database and redis settings, everything else can be left out

| name                     | description                                                                                 |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| `BOT_TOKEN`              | Telegram bot API token                                                                      |
| `RATE_LIMIT`             | Maximum number of requests allowed per minute for rate limiting                             |
| `DEBUG`                  | Enable or disable debugging mode (e.g., `True` or `False`)                                  |
| `USE_WEBHOOK`            | Serve updates via webhook (`bot.entrypoints.api`) instead of polling (e.g., `True` or `False`) |
| `WEBHOOK_BASE_URL`       | Base URL for the webhook                                                                    |
| `WEBHOOK_PATH`           | Path to receive updates from Telegram                                                       |
| `WEBHOOK_SECRET`         | Secret key for securing the webhook communication                                           |
| `WEBHOOK_VERIFY_SOURCE_IP` | Reject webhook requests not sourced from Telegram's published IP ranges (`True`/`False`)  |
| `WEBHOOK_HOST`           | Hostname or IP address for the main application                                             |
| `WEBHOOK_PORT`           | **Host** port published for the `api` service; the container always serves on `8080`        |
| `WEBAPP_URL`             | Public HTTPS URL of the Mini App page (must be HTTPS; omitting it hides the launch button)  |
| `WEBAPP_INIT_DATA_MAX_AGE` | Seconds a captured `initData` stays usable against `/api/webapp/*` (default `3600`)      |
| `ADMIN_ENABLED`          | Mount the admin panel at `/admin` (`True`/`False`); `False` leaves no `/admin` route at all |
| `ADMIN_SECRET_KEY`       | **Required** while `ADMIN_ENABLED=True` — signs the admin session cookie; no default        |
| `DEFAULT_ADMIN_EMAIL`    | Email of the superuser seeded on the first start against an empty `admin` table             |
| `DEFAULT_ADMIN_PASSWORD` | Password for that seeded superuser — change it, and the seeded account's password with it   |
| `DB_HOST`                | Hostname or IP address of the PostgreSQL database                                           |
| `DB_PORT`                | Port the app dials pgbouncer on — a **container** port; keep `5432` under Docker            |
| `DB_USER`                | Username for authenticating with the PostgreSQL database                                    |
| `DB_PASS`                | Password for authenticating with the PostgreSQL database                                    |
| `DB_NAME`                | Name of the PostgreSQL database                                                             |
| `REDIS_HOST`             | Hostname or IP address of the Redis database                                                |
| `REDIS_PORT`             | Port the app dials Redis on — a **container** port; keep `6379` under Docker                |
| `REDIS_PASS`             | Password for authenticating with the Redis database                                         |
| `BACKUP_AGE_PUBLIC_KEY`  | age recipient (`age1...`) the dumps are encrypted to; unset and backups **refuse to run**   |
| `BACKUP_CHAT_ID`         | Private channel the encrypted parts are uploaded to; unset switches backups off             |
| `BACKUP_SCHEDULE`        | Cron for the upload — 5 space-separated fields; `0 3 * * *` by default                       |
| `BACKUP_DIR`             | Where `pgbackup`'s dumps are mounted **read-only** in `worker` (`/backups`)                  |
| `BACKUP_KEEP_DAYS`       | Delete backup messages this bot uploaded once they are older than this (default `30`)       |
| `SENTRY_DSN`             | Sentry DSN (Data Source Name) for error tracking                                            |
| `AMPLITUDE_API_KEY`      | API key for Amplitude analytics                                                             |
| `POSTHOG_API_KEY`        | API key for PostHog analytics                                                               |
| `PROMETHEUS_PORT`        | Port number for the Prometheus monitoring system                                            |
| `GRAFANA_PORT`           | Port number for the Grafana monitoring and visualization platform                           |
| `GRAFANA_ADMIN_USER`     | Admin username for accessing Grafana                                                        |
| `GRAFANA_ADMIN_PASSWORD` | Admin password for accessing Grafana                                                        |

## 📂 Project Folder Structure

```bash
.
├── bot # Source code for Telegram Bot
│   ├── __init__.py
│   ├── __main__.py # Main entry point to launch the bot
│   ├── admin/ # SQLAdmin panel — views, session auth, dashboard; mounted at /admin
│   ├── analytics/ # Interaction with analytics services (e.g., Amplitude or Google Analytics)
│   ├── cache/ # Logic for using Redis cache
│   ├── core/ # Settings for application and other core components
│   ├── database/ # Database functions and SQLAlchemy Models
│   ├── filters/ # Filters for processing incoming messages or updates
│   ├── handlers/ # Handlers for processing user commands and interactions
│   ├── keyboards # Modules for creating custom keyboards
│   │   ├── default_commands.py # Default command keyboards
│   │   ├── __init__.py
│   │   ├── inline/ # Inline keyboards
│   │   └── reply/ # Reply keyboards
│   ├── locales/ # Localization files for supporting multiple languages
│   ├── middlewares/ # Middleware modules for processing incoming updates
│   ├── services/ # Business logic for application
│   └── utils/ # Utility functions and helper modules
│
├── migrations # Database Migrations managed by Alembic
│   ├── env.py # Environment setup for Alembic
│   ├── __init__.py
│   ├── README
│   ├── script.py.mako # Script template for generating migrations
│   └── versions/ # Folder containing individual migration scripts
│
├── configs # Config folder for Monitoring (Prometheus, Node-exporter and Grafana)
│   ├── grafana # Configuration files for Grafana
│   │   ├── dashboards/local.yml # Provisioning: scan /var/lib/grafana/dashboards
│   │   ├── datasources/datasource.yml # Provisioning: the Prometheus datasource
│   │   ├── node-exporter.json # Dashboard: host CPU, memory, disk and network
│   │   └── tgbot.json # Dashboard: the bot's own tgbot_* metrics
│   └── prometheus # Configuration files for Prometheus
│       └── prometheus.yml
│
├── scripts/ # Sripts folder
├── Makefile # List of commands for standard
├── alembic.ini # Configuration file for migrations
├── docker-compose.yml # Docker Compose configuration file for orchestrating containers
├── Dockerfile # Dockerfile for Telegram Bot
├── LICENSE.md # License file for the project
├── uv.lock # Lock file for UV dependency management
├── pyproject.toml # Configuration file for Python projects, including build tools, dependencies, and metadata
└── README.md # Documentation
```

## 🔧 Tech Stack

-   `sqlalchemy` — object-relational mapping (ORM) library that provides a set of high-level API for interacting with relational databases
-   `asyncpg` — asynchronous PostgreSQL database client library
-   `aiogram` — asynchronous framework for Telegram Bot API
-   `sqladmin` — admin panel over the SQLAlchemy models, mounted on the FastAPI app
-   `loguru` — third party library for logging in Python
-   `uv` — development workflow
-   `docker` — to automate deployment
-   `postgres` — powerful, open source object-relational database system
-   `pgbouncer` — connection pooler for PostgreSQL database, in transaction pooling mode
-   `redis` — in-memory data structure store used as a cache and FSM
-   `age` — file encryption for the database backups shipped to Telegram
-   `prometheus` — time series database for collecting metrics from various systems
-   `grafana` — visualization and analysis from various sources, including Prometheus

## 🧪 Continuous integration

Besides linting and building the image, CI boots the compose stack on every push and pull
request to `main` ([`.github/workflows/compose-smoke.yml`](.github/workflows/compose-smoke.yml)).
It runs as two jobs, because the API cannot start without a real bot token — `lifespan`
calls `getMe` and lets the failure escape, so with a fake token the container exits before
serving anything:

| Job | Needs | Proves |
| --- | --- | --- |
| `smoke` | nothing | the image builds; `postgres`, `pgbouncer` and `redis` come up healthy; the migrator exits 0; `worker` and `scheduler` come up healthy; the scheduled tasks are registered on the broker **and** picked up by the schedule source |
| `smoke-api` | a `BOT_TOKEN` repository secret | the `api` service comes up healthy and serves `/health/live`, `/health/ready`, `/metrics`, `/webapp` and `/admin` |

**Without that secret, CI never starts the application.** `smoke` on its own is green while
nothing has checked that the API serves a single request — it says the workers and the
database layer are fine, and nothing more. The job prints exactly that as a notice on every
run, and `smoke-api` prints a notice when it skips.

To turn `smoke-api` on, create a **throwaway** bot with [@BotFather](https://t.me/BotFather)
and store its token as the `BOT_TOKEN` repository secret (_Settings → Secrets and variables →
Actions → New repository secret_). It must not be a production bot: CI starts a real bot
process against it and calls `setMyCommands`, which rewrites that bot's command menu. The job
keeps `USE_WEBHOOK=False` so it never repoints the bot's webhook.

Secrets are unavailable to workflows triggered from a fork, by design, so `smoke-api` does
not run on fork pull requests — it is skipped rather than failed.

## ⭐ Star History

[![Star History Chart](https://star-history.dera.page/svg?repos=donbarbos/telegram-bot-template&type=Date)](https://star-history.dera.page/#donbarbos/telegram-bot-template&Date)

## 👷 Contributing

First off, thanks for taking the time to contribute! Contributions are what makes the open-source community such an amazing place to learn, inspire, and create. Any contributions you make will benefit everybody else and are greatly appreciated.

If you have a suggestion that would make this better, please fork the repo and create a pull request. You can also simply open an issue with the tag "enhancement". Don't forget to give the project a star! Thanks again!

1. `Fork` this repository
2. Create a `branch`
3. `Commit` your changes
4. `Push` your `commits` to the `branch`
5. Submit a `pull request`

## 📝 License

Distributed under the MIT license. See [`LICENSE`](./LICENSE.md) for more information.

## 📢 Contact

[donbarbos](https://github.com/donbarbos): donbarbos@proton.me
