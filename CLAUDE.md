# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (uv only — never bare pip)
uv sync --frozen --all-groups        # all groups: bot + admin + dev
make deps                            # uv sync --frozen (runtime deps only)

# Run locally (needs Postgres + Redis reachable per .env)
uv run python -m bot                 # Telegram bot (polling, or webhook if USE_WEBHOOK=True)
uv run gunicorn -c admin/gunicorn_conf.py   # Flask admin panel

# Lint / typecheck (what CI runs — .github/workflows/linters.yml)
make check                           # ruff check . && ruff format --check .
make format                          # ruff check --fix . && ruff format .
uv run mypy

# Migrations (local)
uv run alembic revision --autogenerate -m "message"
uv run alembic upgrade head
uv run alembic downgrade -1
# Migrations (inside docker compose)
make mm args="message"  /  make migrate  /  make downgrade args=-1

# i18n — .mo files are gitignored, so compile before running locally or _() falls back to msgid
make babel-extract && make babel-update && make babel-compile

# Docker (full stack: bot, admin, postgres, pgbouncer, redis, migrator, prometheus, grafana)
make compose-up / compose-down / compose-ps
make logs args=bot
```

There are **no tests and no test runner** in this repo (no pytest dependency, no `tests/`). Do not add or run tests unless asked.

## Architecture

Two independent applications share one Postgres database and one set of SQLAlchemy models:

- **`bot/`** — aiogram 3 bot, fully async (asyncpg + SQLAlchemy 2 async, uvloop).
- **`admin/`** — Flask-Admin panel, fully sync (psycopg2 + Flask-SQLAlchemy). It imports `bot.database.models.UserModel` directly; this is the only cross-app coupling.

Dependency groups in `pyproject.toml` (`bot`, `admin`, `dev`) are installed separately by the two Dockerfiles (`Dockerfile` = bot, `admin/Dockerfile` = admin), so **a bot-only library is not available in the admin process and vice versa**.

### Update pipeline (order matters, `bot/middlewares/__init__.py`)

`ThrottlingMiddleware` (message outer, TTLCache keyed by chat, `RATE_LIMIT` seconds) → `LoggingMiddleware` (update outer) → `DatabaseMiddleware` (update outer, opens a session and puts it in `data["session"]`) → `AuthMiddleware` (message inner, auto-registers unknown users and captures the `/start <referrer>` argument) → `ACLMiddleware` (i18n; reads the user's `language_code` from the DB) → `CallbackAnswerMiddleware`.

Consequences:
- Any handler or filter that needs the DB just declares `session: AsyncSession` as a parameter — it is injected, never constructed. See `bot/filters/admin.py`.
- `ACLMiddleware` and `AuthMiddleware` run *after* `DatabaseMiddleware`, so they can rely on `data["session"]`. Anything inserted before it cannot.
- Users are created implicitly by middleware, not by the `/start` handler.

### Adding a handler

Handler modules each expose `router = Router(name=...)`. Register them in `get_handlers_router()` (`bot/handlers/__init__.py`), which imports the handler modules at file top level. Same for `register_middlewares()` in `bot/middlewares/__init__.py`. Note that `bot/core/loader.py` builds `bot`, `dp`, `redis_client`, `storage`, `i18n`, and the aiohttp `app` at import time, so importing anything under `bot/` instantiates those; the routers are module-level singletons, so `get_handlers_router()` may only be called once per process.

### Services + Redis cache

Business logic lives in `bot/services/`; handlers stay thin. Read functions are decorated with `@cached(key_builder=lambda session, user_id: build_key(user_id))` — the session is deliberately excluded from the cache key, and TTL defaults to 10s (`bot/cache/redis.py`, pickle-serialized). **Any write must invalidate explicitly**: `await clear_cache(user_exists, user_id)` — the key is derived from `func.__module__:func.__name__`, so renaming or moving a cached function silently orphans its keys.

### i18n

Text uses `_()` from `aiogram.utils.i18n`, resolved per-update by `ACLMiddleware` from the user's DB `language_code` (falling back to `DEFAULT_LOCALE = "en"`). Locales: `en`, `ru`, `uk` under `bot/locales/`. `.mo` files are gitignored and compiled at Docker build time; locally run `make babel-compile`. (`make babel` is broken — it depends on nonexistent `extract`/`update` targets; use the `babel-*` targets directly.)

### Analytics

`analytics.track_event("<EventType>")` decorates handlers *below* the router decorator (`bot/handlers/start.py`). `EventType` is a closed `Literal` in `bot/analytics/types.py` — add new event names there first. `AnalyticsService` is a singleton over an `AbstractAnalyticsLogger`; Amplitude is wired up by default, PostHog and Google clients exist in `bot/analytics/`. Handler exceptions are reported as an `"Error"` event and re-raised.

### Config

`bot/core/config.py` composes `Settings` by multiple inheritance (`BotSettings(WebhookSettings)`, `DBSettings`, `CacheSettings`) via pydantic-settings reading `.env`, resolved to an absolute path (`f"{DIR}/.env"`) so it is found regardless of the working directory. `BOT_TOKEN` and `AMPLITUDE_API_KEY` have no defaults — the bot will not import without them. The admin panel does **not** use this; it has its own `os.getenv`-based `admin/config.py` loaded by `app.config.from_pyfile`.

`USE_WEBHOOK` switches between `dp.start_polling` and an aiohttp server; the Prometheus middleware and `/metrics` endpoint are only mounted in webhook mode (`bot/__main__.py`).

### Database, migrations, pgbouncer

- Models inherit `Base` (`bot/database/models/base.py`) with annotated column types (`big_int_pk`, `created_at`) and a `repr_cols` convention. A new model **must** be re-exported from `bot/database/models/__init__.py` — Alembic autogenerate reads `Base.metadata` through that import and would otherwise emit a drop.
- `migrations/` is excluded from ruff.
- Alembic manages only the bot's models. The admin panel's `admin` / `role` / `roles_admins` tables are created outside Alembic by `init_db()` in `admin/app.py` on first startup, which also seeds the default superuser from `DEFAULT_ADMIN_EMAIL`/`DEFAULT_ADMIN_PASSWORD`.
- The engine uses `pool_size=0` and a custom `CConnection` that randomizes asyncpg's prepared-statement names (`bot/database/database.py`). This exists because traffic goes through **pgbouncer** — do not "clean up" either.

## Conventions

- Ruff runs with `lint.select = ["ALL"]`, line-length 120, `fix = true`, `unsafe-fixes = true`. Because `fix = true` lives in the config, a bare `ruff check .` **rewrites files** rather than just reporting — CI's "0 remaining" can hide edits. Practically this means: full type annotations on every function, `from __future__ import annotations` plus `if TYPE_CHECKING:` blocks for typing-only imports (the codebase does this everywhere), and targeted `# ruff: noqa: <CODE>` at the top of a file when a rule genuinely doesn't fit.
- mypy is configured strictly but `files = "bot/*.py"` matches only top-level modules — passing `uv run mypy` is not evidence that a change in `bot/services/` typechecks.
- Logging uses **loguru** (`from loguru import logger`), never stdlib `logging`. Sentry is wired to loguru when `SENTRY_DSN` is set.
- Code targets Python **3.14** (`requires-python = ">=3.14,<4.0"`, pinned by `.python-version`); Docker runs `ghcr.io/astral-sh/uv:0.12-python3.14-alpine`.
- **`ruff` has `fix = true` but `unsafe-fixes = false`.** The unsafe flag was on and ruff 0.16 used it to move runtime-needed imports into `TYPE_CHECKING`, breaking `get_type_hints()` on the aiogram filters. `docs/` is excluded because ruff now formats Python blocks inside markdown.
- Everything runs through `uv`: `scripts/*` use `uv run`, and a `uv-lock` pre-commit hook keeps `uv.lock` in step with `pyproject.toml`.
- Naming: handlers `<name>_handler`, filters `<Name>Filter`, middlewares `<Name>Middleware`, models `<Name>Model`, keyboards `<name>_keyboard`.

## Fork / upstream

`origin` is `ganiyevuz/telegram-bot-template`; `upstream` is `donbarbos/telegram-bot-template`. Sync with `git fetch upstream && git merge upstream/main`.

The fork carries four deliberate deviations from upstream — keep them when merging:

- `admin/app.py` uses the flask-admin 2.x API (`theme=Bootstrap4Theme(base_template=...)` and `admin.theme.base_template`). Upstream bumped flask-admin 1.6 → 2.2 without porting the call sites, so upstream's `admin/app.py` raises `TypeError` at import.
- `pyproject.toml` declares `sqlalchemy[asyncio]`. Without the extra, SQLAlchemy only pulls in `greenlet` on the platform machines it enumerates (`aarch64`, `x86_64`, `amd64`, …); **macOS Apple Silicon reports `arm64`**, so a native `uv sync` there omitted it and every async query died with `ValueError: the greenlet library is required`. Docker images were unaffected (Linux reports `aarch64`), which is why upstream never hit it.
- `uv.lock` pins `aiogram` 3.29.1, not the 3.29.0 upstream locked — that release was yanked from PyPI ("severe slowdown on parsing nested RichBlock entities"). 3.30.0 is also available if you want to move up the line.
- `bot/middlewares/prometheus.py` has no `# noqa: BLE001` (unused under ruff 0.15, and `fix = true` strips it on every lint run).

Known upstream quirks, unchanged here:

- The `hasattr(event, "chat_member")` guard in `bot/middlewares/i18n.py` is dead code. `I18nMiddleware.setup()` attaches to every observer *except* `update`, and only `Update` has a `chat_member` field — so the guard never fires.
- `alembic check` reports drift (`add_constraint UniqueConstraint on users.id`) because `big_int_pk` sets `unique=True` on a primary key. Cosmetic; don't "fix" it with a migration.
