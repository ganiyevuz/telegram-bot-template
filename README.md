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

-   [x] Admin Panel based on [`Flask-Admin-Dashboard`](https://github.com/jonalxh/Flask-Admin-Dashboard/) ([`Flask-Admin`](https://flask-admin.readthedocs.io/) + [`AdminLTE`](https://adminlte.io/) = ❤️ )
-   [x] Product Analytics System: using [`Amplitude`](https://amplitude.com/) or [`Posthog`](https://posthog.com/) or [`Google Analytics`](https://analytics.google.com)
-   [x] Performance Monitoring System: using [`Prometheus`](https://prometheus.io/) and [`Grafana`](https://grafana.com/)
-   [x] Tracking System: using [`Sentry`](https://sentry.io/)
-   [x] Seamless use of `Docker` and `Docker Compose`
-   [x] Export all users in `.csv` (or `.xlsx`, `.json`, `yaml` from admin panel)
-   [x] Configured CI pipeline from git hooks to github actions
-   [x] [`SQLAlchemy V2`](https://pypi.org/project/SQLAlchemy/) is used to communicate with the database
-   [x] Database Migrations with [`Alembic`](https://pypi.org/project/alembic/)
-   [x] Ability to cache using decorator
-   [x] Convenient validation using [`Pydantic V2`](https://pypi.org/project/pydantic/)
-   [x] Internationalization (i18n) using GNU gettex and [`Babel`](https://pypi.org/project/Babel/)

## 🚀 How to Use

### 🐳 Running in Docker _(recommended method)_

-   configure environment variables in `.env` file

-   start services

    ```bash
    docker compose up -d --build
    ```

### 💻 Running on Local Machine

-   set environment and install dependencies using [uv](https://docs.astral.sh/uv/ "python package manager") (you can find branch with Poetry [here](https://github.com/donbarbos/telegram-bot-template/tree/poetry-archive))

    ```bash
    uv sync --frozen --all-groups
    ```

-   start the necessary services (at least your database and redis)

-   configure environment variables in `.env` file

-   start telegram bot

    ```bash
    uv run python -m bot
    ```

-   start admin panel

    ```bash
    uv run gunicorn -c admin/gunicorn_conf.py
    ```

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
| `WEBHOOK_PORT`           | Port number for the main application                                                        |
| `WEBAPP_URL`             | Public HTTPS URL of the Mini App page (must be HTTPS; omitting it hides the launch button)  |
| `WEBAPP_INIT_DATA_MAX_AGE` | Seconds a captured `initData` stays usable against `/api/webapp/*` (default `3600`)      |
| `ADMIN_HOST`             | Hostname or IP address for the admin panel                                                  |
| `ADMIN_PORT`             | Port number for the admin panel                                                             |
| `DEFAULT_ADMIN_EMAIL`    | Default email for the admin user                                                            |
| `DEFAULT_ADMIN_PASSWORD` | Default password for the admin user                                                         |
| `SECURITY_PASSWORD_HASH` | Hashing algorithm for user passwords (e.g., `bcrypt`)                                       |
| `SECURITY_PASSWORD_SALT` | Salt value for user password hashing                                                        |
| `DB_HOST`                | Hostname or IP address of the PostgreSQL database                                           |
| `DB_PORT`                | Port number for the PostgreSQL database                                                     |
| `DB_USER`                | Username for authenticating with the PostgreSQL database                                    |
| `DB_PASS`                | Password for authenticating with the PostgreSQL database                                    |
| `DB_NAME`                | Name of the PostgreSQL database                                                             |
| `REDIS_HOST`             | Hostname or IP address of the Redis database                                                |
| `REDIS_PORT`             | Port number for the Redis database                                                          |
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
├── admin # Source code for admin panel
│   ├── __init__.py
│   ├── app.py # Main application module for the admin panel
│   ├── config.py # Configuration module for the admin panel
│   ├── Dockerfile # Dockerfile for admin panel
│   ├── gunicorn_conf.py # Gunicorn configuration file for serving admin panel
│   ├── static # Folder for static assets
│   │   ├── css/
│   │   ├── fonts/
│   │   ├── img/
│   │   ├── js/
│   │   └── plugins/
│   ├── templates # HTML templates for the admin panel
│   │   ├── admin/
│   │   ├── index.html
│   │   ├── my_master.html
│   │   └── security/
│   └── views # Custom View modules for handling web requests
│       ├── __init__.py
│       └── users.py
│
├── bot # Source code for Telegram Bot
│   ├── __init__.py
│   ├── __main__.py # Main entry point to launch the bot
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
│   │   └── datasource.yml
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
-   `flask-admin` — simple and extensible administrative interface framework
-   `loguru` — third party library for logging in Python
-   `uv` — development workflow
-   `docker` — to automate deployment
-   `postgres` — powerful, open source object-relational database system
-   `pgbouncer` — connection pooler for PostgreSQL database
-   `redis` — in-memory data structure store used as a cache and FSM
-   `prometheus` — time series database for collecting metrics from various systems
-   `grafana` — visualization and analysis from various sources, including Prometheus

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
