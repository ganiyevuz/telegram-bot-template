# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (uv only — never bare pip)
uv sync --frozen --all-groups        # all groups: bot + dev
make deps                            # uv sync --frozen (runtime deps only)

# Run locally (needs Postgres + Redis reachable per .env)
uv run python -m bot                 # Telegram bot, long polling only (no HTTP at all)
uv run uvicorn bot.entrypoints.api:app --port 8080   # API + Mini App + admin panel at /admin

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

# Docker (full stack: api, postgres, pgbouncer, redis, migrator, prometheus, grafana)
make compose-up / compose-down / compose-ps
make logs args=api
```

There are **no tests and no test runner** in this repo (no pytest dependency, no `tests/`). Do not add or run tests unless asked.

## Architecture

One application, fully async (asyncpg + SQLAlchemy 2 async, uvloop), with two entrypoints over one dishka container:

- `bot/entrypoints/polling.py` (`python -m bot`) — long polling. Development only; serves no HTTP.
- `bot/entrypoints/api.py` — the FastAPI app: `/webhook`, `/health/*`, `/metrics`, the Mini App at `/webapp` + `/api/webapp/*`, and the **admin panel at `/admin`**. This is the deployment path and the only horizontally scalable one.

There is no second application and no second dependency set. The panel used to be a parallel synchronous Flask-Admin stack under `admin/` with nine dependencies of its own; it is now `bot/admin/` — SQLAdmin views bound to the same models, the same `AsyncEngine` and the same connection pool the bot runs on. `pyproject.toml` has two dependency groups left, `bot` and `dev`, and one Dockerfile.

### Admin panel (`bot/admin/`)

- `setup_admin(app, engine, sessionmaker, settings)` is called from `_lifespan` in `bot/entrypoints/api.py`, not from `create_app()` — the engine and sessionmaker are APP-scoped dishka objects that do not exist until the container is built. Mounting there is fine (it appends to a route list Starlette re-reads per request); `add_middleware` is what cannot happen after startup, which is why `ContainerMiddleware` is registered in `create_app()` and the container attached in the lifespan. The login cookie's `SessionMiddleware` is installed by SQLAdmin into its own Starlette sub-app, so it needs nothing from `create_app()`.
- Passwords are `scrypt$<salt>$<digest>` from `bot/admin/security.py` (stdlib `hashlib.scrypt`), verified with `hmac.compare_digest`. `AdminAuth` re-reads the admin row on **every** request, so deactivating an account or revoking `superuser` takes effect immediately rather than at session expiry.
- `ADMIN_SECRET_KEY` has **no default** and `_lifespan` refuses to start without one while `ADMIN_ENABLED=True` — same shape as the `WEBHOOK_SECRET` guard above it. `ADMIN_ENABLED=False` leaves no `/admin` route at all.
- The default superuser is seeded by `seed_default_admin` in the lifespan, once, and only against an empty `admin` table.
- SQLAdmin derives each view's URL segment from the **model class name** (`ModelViewMeta` overwrites `identity` with `slugify_class_name(model.__name__)`), so the lists live at `/admin/user-model/list`, `/admin/payment-model/list`, `/admin/admin-model/list`, `/admin/role-model/list`. Setting `identity` in the class body does nothing.
- `DashboardView` is a `BaseView` exposed at `/`, which collides with SQLAdmin's own built-in index route; `_promote_dashboard_to_index` reorders it ahead of that route so `/admin/` renders the dashboard. Do not "clean that up" — deleting the built-in route breaks `url_for("admin:index")` in SQLAdmin's own layout.

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

`bot/core/config.py` composes `Settings` from nested pydantic-settings classes (`BotSettings`, `DatabaseSettings`, `WebhookSettings`, `AdminSettings`, ...) reading `.env`, resolved to an absolute path (`f"{DIR}/.env"`) so it is found regardless of the working directory. `BOT_TOKEN` has no default — nothing under `bot/` imports without it. The admin panel reads this same `Settings` (`settings.admin`); there is no second configuration module.

`USE_WEBHOOK` switches between `dp.start_polling` and an aiohttp server; the Prometheus middleware and `/metrics` endpoint are only mounted in webhook mode (`bot/__main__.py`).

### Database, migrations, pgbouncer

- Models inherit `Base` (`bot/database/models/base.py`) with annotated column types (`big_int_pk`, `created_at`) and a `repr_cols` convention. A new model **must** be re-exported from `bot/database/models/__init__.py` — Alembic autogenerate reads `Base.metadata` through that import and would otherwise emit a drop.
- `migrations/` is excluded from ruff.
- Alembic manages **every** table, the panel's `admin` / `role` / `roles_admins` included (`migrations/versions/2026-08-23_admin_tables.py`). Those three used to be created outside Alembic by `init_db()` in the deleted `admin/app.py`; that migration refuses to run on a database still carrying them and prints the `DROP TABLE` to run first, because their Flask-Security `pbkdf2_sha512` digests cannot be verified by the new scrypt hashing.
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

- The Flask-Admin panel is **gone** — `admin/`, the `admin` compose service and nine dependencies (flask, flask-admin, flask-security-too, flask-caching, flask-babel, flask-sqlalchemy, psycopg2-binary, gunicorn, tablib) were deleted and replaced by `bot/admin/` (SQLAdmin). Upstream still ships it, and its `admin/app.py` still raises `TypeError` at import on flask-admin 2.x; a merge from upstream will try to restore the whole directory. Delete it again rather than porting it.
- `pyproject.toml` declares `sqlalchemy[asyncio]`. Without the extra, SQLAlchemy only pulls in `greenlet` on the platform machines it enumerates (`aarch64`, `x86_64`, `amd64`, …); **macOS Apple Silicon reports `arm64`**, so a native `uv sync` there omitted it and every async query died with `ValueError: the greenlet library is required`. Docker images were unaffected (Linux reports `aarch64`), which is why upstream never hit it.
- `uv.lock` pins `aiogram` 3.29.1, not the 3.29.0 upstream locked — that release was yanked from PyPI ("severe slowdown on parsing nested RichBlock entities"). 3.30.0 is also available if you want to move up the line.
- `bot/middlewares/prometheus.py` has no `# noqa: BLE001` (unused under ruff 0.15, and `fix = true` strips it on every lint run).

Known upstream quirks, unchanged here:

- The `hasattr(event, "chat_member")` guard in `bot/middlewares/i18n.py` is dead code. `I18nMiddleware.setup()` attaches to every observer *except* `update`, and only `Update` has a `chat_member` field — so the guard never fires.
- `alembic check` reports drift (`add_constraint UniqueConstraint on users.id`) because `big_int_pk` sets `unique=True` on a primary key. Cosmetic; don't "fix" it with a migration.
