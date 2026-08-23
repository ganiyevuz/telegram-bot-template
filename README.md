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
