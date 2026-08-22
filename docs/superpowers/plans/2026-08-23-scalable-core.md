# Scalable Core (Phases 1-6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the bot's runtime so N stateless replicas can serve webhooks concurrently, with no correctness-critical state in process memory.

**Architecture:** Import-time globals are replaced by a dishka DI container with APP and REQUEST scopes. Free-function services become repositories plus use-case objects that own their own cache invalidation. In-process throttling, deduplication, and outbound rate limiting all move to atomic Redis Lua scripts so they hold across replicas. Slow work moves to TaskIQ workers.

**Tech Stack:** Python 3.12+, aiogram 3.30, dishka 1.10, FastAPI 0.141 + uvicorn 0.52, TaskIQ 0.12 + taskiq-redis 1.2, SQLAlchemy 2.0.52 (asyncio), asyncpg, Redis 8, Alembic 1.19, loguru, prometheus-client, uv.

**Spec:** `docs/superpowers/specs/2026-08-23-scalable-architecture-design.md` — read sections 1-11 and the "Plan scoping" block in section 19 before starting. This plan implements phases 1-6 only.

## Global Constraints

- **No tests.** The user explicitly scoped this work as "architecture only" and has a standing rule: do not write or run tests until asked. Do **not** add pytest, create a `tests/` directory, or write test files. Each task is verified by running the real code against live Postgres and Redis, then `ruff` and `mypy`. If you believe a task needs a test, say so and stop — do not write one.
- **The Flask admin panel must keep working after every task.** It is not replaced until phase 10, which is out of scope here. Therefore: SQLAlchemy models stay at `bot/database/models/`; the `users` table shape does not change; `admin/`, `admin/Dockerfile`, and the `admin` compose service are not touched.
- **Dependency removals are restricted.** Only `cachetools` and `types-cachetools` may be removed in this plan (Task 10). `flask`, `flask-admin`, `flask-security-too`, `flask-caching`, `flask-babel`, `flask-sqlalchemy`, `psycopg2-binary`, `gunicorn`, and `tablib` are still used by the admin panel and MUST remain.
- **Python floor is 3.14** and is already set. `requires-python = ">=3.14,<4.0"`. Use modern syntax (`X | None`, `type` statements where useful).
- **`unsafe-fixes` is off, deliberately.** It was `true`, and ruff 0.16 used it to move runtime-needed imports into `TYPE_CHECKING` blocks, breaking `get_type_hints()` on the aiogram filters. Do not turn it back on.
- **Package manager is `uv`, never bare pip.** Use `uv add` / `uv sync` / `uv run`. Always run `uv sync` after editing `pyproject.toml`.
- **Logging is loguru.** `from loguru import logger`. Never stdlib `logging` in new code.
- **Ruff runs with `lint.select = ["ALL"]`, line length 120, and `fix = true` in config** — a bare `ruff check .` rewrites files. Always re-check `git diff` after linting.
- **Environment variable names are preserved.** `BOT_TOKEN`, `DB_HOST`, `REDIS_PORT` etc. keep their current spelling. Any rename must be recorded in `.env.example`.
- **Latest stable versions only.** No pre-releases (`sqlalchemy` 2.1.0bX, `pydantic` 2.14.0bX, `sentry-sdk` 3.0.0aX are excluded).
- **Commit after every task**, using Conventional Commit prefixes. Never add a `Co-Authored-By` trailer.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `bot/core/logging.py` | loguru configuration, correlation-id contextvar, stdlib bridge |
| `bot/core/di.py` | dishka providers — the composition root |
| `bot/core/lifespan.py` | shared startup/shutdown for every entrypoint |
| `bot/telegram/factory.py` | builds the `Bot`, attaches session middlewares |
| `bot/telegram/ratelimit.py` | aiogram session middleware enforcing Telegram's send limits |
| `bot/cache/keys.py` | typed cache-key registry — single source of truth for keys |
| `bot/cache/service.py` | `CacheService`: get/set/delete/invalidate over orjson |
| `bot/cache/ratelimit.py` | `TokenBucket` — atomic Redis Lua primitive, used inbound and outbound |
| `bot/database/repositories/user.py` | `UserRepository` — all `users` table access |
| `bot/middlewares/types.py` | typed `MiddlewareData` for context keys |
| `bot/middlewares/dedup.py` | `SET NX` guard against webhook redelivery |
| `bot/middlewares/metrics.py` | RED metrics per handler |
| `bot/handlers/errors.py` | global `dp.errors` handler |
| `bot/api/health.py` `bot/api/metrics.py` `bot/api/webhook.py` | FastAPI routes |
| `bot/entrypoints/{api,worker,scheduler,polling}.py` | one process role each |
| `bot/tasks/{__init__,analytics,export,broadcast}.py` | TaskIQ broker and jobs |
| `scripts/devstack` | throwaway Postgres + Redis for verification |

**Deleted:** `bot/core/loader.py`, `bot/cache/redis.py`, `bot/middlewares/database.py`, `bot/handlers/metrics.py`, `bot/database/database.py` (folded into `core/di.py`).

**Heavily modified:** `bot/core/config.py`, `bot/services/users.py`, `bot/middlewares/{__init__,auth,i18n,logging,throttling}.py`, `bot/utils/users_export.py`, `bot/handlers/export_users.py`, `bot/__main__.py`, `pyproject.toml`.

---

### Task 1: Dependencies and the verification harness

> **Already landed** (commit preceding this plan): the Python 3.14 floor,
> `.python-version`, the uv 0.12 / Python 3.14 Docker images, the CI matrix,
> `mypy` widened to the whole `bot` package, `ruff` 0.16.4 / `mypy` 2.3.1 /
> `pre-commit` 4.6.2, `unsafe-fixes = false`, the uv-based `scripts/*`, and the
> `uv-lock` pre-commit hook. Skip those steps below; they are kept for context.

**Files:**
- Modify: `pyproject.toml`
- Create: `scripts/devstack`

**Interfaces:**
- Consumes: nothing.
- Produces: `scripts/devstack up` exporting `DB_*`/`REDIS_*` env vars for every later task; dependency availability for all later tasks.

- [ ] **Step 1: Raise the Python floor and add runtime dependencies**

Edit `pyproject.toml`. Change `requires-python`:

```toml
requires-python = ">=3.12,<4.0"
```

Add to the `bot` dependency group (keep every existing entry):

```toml
    "dishka>=1.10.1,<2.0.0",
    "fastapi>=0.141.1,<1.0.0",
    "uvicorn[standard]>=0.52.4,<1.0.0",
    "taskiq>=0.12.5,<1.0.0",
    "taskiq-redis>=1.2.3,<2.0.0",
```

Bump these existing pins in place:

```toml
    "aiogram>=3.30.0,<4.0.0",
    "orjson>=3.12.0,<4.0.0",
    "prometheus-client>=0.26.0,<1.0.0",
    "redis>=8.1.0,<9.0.0",
    "alembic>=1.19.1,<2.0.0",
    "sentry-sdk[loguru]>=2.68.0,<3.0.0",
    "aiohttp[speedups]>=3.14.3,<4.0.0",
```

And in `[project.dependencies]`:

```toml
    "sqlalchemy[asyncio]>=2.0.52,<3.0.0",
    "pydantic>=2.13.4,<3.0.0",
    "pydantic-settings>=2.15.0,<3.0.0",
```

And in the `dev` group:

```toml
    "ruff>=0.16.4,<1.0.0",
    "mypy>=2.3.1,<3.0.0",
    "pre-commit>=4.6.2,<5.0.0",
```

Do **not** remove `cachetools` or `types-cachetools` yet — the current throttler still uses them until Task 10.

- [ ] **Step 2: Update the mypy target and the CI matrix**

In `pyproject.toml`, under `[tool.mypy]`, change `python_version` and widen the checked surface — `files = "bot/*.py"` currently matches only top-level modules, so CI's mypy run checks almost nothing:

```toml
python_version = "3.12"
files = "bot"
```

In `.github/workflows/linters.yml`, replace the matrix with the real floor:

```yaml
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
```

- [ ] **Step 3: Sync and confirm resolution**

```bash
uv lock && uv sync --frozen --all-groups
uv run python -c "import fastapi, uvicorn, dishka, taskiq, taskiq_redis, aiogram; print('deps ok', aiogram.__version__)"
```

Expected: `deps ok 3.30.0`. If `uv lock` warns that a version is yanked, pick the next stable release and note it in the commit message.

- [ ] **Step 4: Create the verification harness**

Every later task verifies against real Postgres and Redis. Create `scripts/devstack`:

```bash
#!/bin/sh -e
# Throwaway Postgres + Redis for local verification.
#   eval "$(scripts/devstack up)"   # start and export DB_*/REDIS_* env vars
#   scripts/devstack down           # remove containers
PG=tbt-devstack-pg
RD=tbt-devstack-redis

case "$1" in
  up)
    docker rm -f $PG $RD >/dev/null 2>&1 || true
    docker run -d --name $PG -e POSTGRES_PASSWORD=dev -e POSTGRES_USER=dev \
      -e POSTGRES_DB=dev -p 55432:5432 postgres:14-alpine >/dev/null
    docker run -d --name $RD -p 56379:6379 redis:7-alpine >/dev/null
    i=0
    while [ $i -lt 30 ]; do
      docker exec $PG pg_isready -U dev >/dev/null 2>&1 && break
      i=$((i+1)); sleep 1
    done
    echo "export DB_HOST=localhost DB_PORT=55432 DB_USER=dev DB_PASS=dev DB_NAME=dev"
    echo "export REDIS_HOST=localhost REDIS_PORT=56379"
    echo "export BOT_TOKEN=110201544:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    ;;
  down)
    docker rm -f $PG $RD >/dev/null 2>&1 || true
    echo "devstack down"
    ;;
  *) echo "usage: scripts/devstack up|down" >&2; exit 2 ;;
esac
```

```bash
chmod +x scripts/devstack
```

The bot token above is Telegram's public documentation example. It is well-formed so `Bot()` accepts it, and it is not a real credential.

- [ ] **Step 5: Verify the harness works**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio, asyncpg, redis.asyncio as r, os
async def main():
    c = await asyncpg.connect(host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']),
                              user=os.environ['DB_USER'], password=os.environ['DB_PASS'],
                              database=os.environ['DB_NAME'])
    print('postgres:', await c.fetchval('select 1')); await c.close()
    rc = r.Redis(host=os.environ['REDIS_HOST'], port=int(os.environ['REDIS_PORT']))
    print('redis:', await rc.ping()); await rc.aclose()
asyncio.run(main())"
```

Expected: `postgres: 1` then `redis: True`.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && git diff --stat
git add pyproject.toml uv.lock scripts/devstack .github/workflows/linters.yml
git commit -m "build: add DI/web/worker deps, raise python floor to 3.12, add devstack"
```

---

### Task 2: Configuration restructure

**Files:**
- Modify: `bot/core/config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces: `get_settings() -> Settings`; `Settings` with attributes `.bot`, `.db`, `.redis`, `.webhook`, `.analytics`, `.observability`, `.debug`. Field paths used by later tasks: `settings.bot.token` (`SecretStr`), `settings.bot.rate_limit` (`float`), `settings.bot.support_url` (`str | None`), `settings.db.url` (`str`), `settings.redis.url` (`str`), `settings.webhook.enabled` (`bool`), `settings.webhook.url` (`str`), `settings.webhook.path` (`str`), `settings.webhook.secret` (`str`), `settings.webhook.host` (`str`), `settings.webhook.port` (`int`), `settings.analytics.amplitude_api_key` (`str | None`), `settings.observability.sentry_dsn` (`str | None`).

- [ ] **Step 1: Replace `bot/core/config.py`**

The current file composes one flat `Settings` by multiple inheritance and requires `AMPLITUDE_API_KEY` with no default, so the bot cannot even import without it. Replace the whole file:

```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DIR = Path(__file__).absolute().parent.parent.parent
BOT_DIR = Path(__file__).absolute().parent.parent
LOCALES_DIR = f"{BOT_DIR}/locales"
I18N_DOMAIN = "messages"
DEFAULT_LOCALE = "en"

_ENV = SettingsConfigDict(env_file=f"{DIR}/.env", env_file_encoding="utf-8", extra="ignore")


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="BOT_")

    token: SecretStr
    support_url: str | None = Field(default=None, validation_alias="SUPPORT_URL")
    rate_limit: float = Field(default=0.5, validation_alias="RATE_LIMIT")


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="DB_")

    host: str = "postgres"
    port: int = 5432
    user: str = "postgres"
    password: SecretStr | None = Field(default=None, validation_alias="DB_PASS")
    name: str = "postgres"
    pool_size: int = 10
    max_overflow: int = 5

    @property
    def url(self) -> str:
        auth = self.user if self.password is None else f"{self.user}:{self.password.get_secret_value()}"
        return f"postgresql+asyncpg://{auth}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="REDIS_")

    host: str = "redis"
    port: int = 6379
    password: SecretStr | None = Field(default=None, validation_alias="REDIS_PASS")
    db: int = 0

    @property
    def url(self) -> str:
        auth = "" if self.password is None else f":{self.password.get_secret_value()}@"
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class WebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="WEBHOOK_")

    enabled: bool = Field(default=False, validation_alias="USE_WEBHOOK")
    base_url: str = "https://example.com"
    path: str = "/webhook"
    secret: str = ""
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.path}"


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV)

    amplitude_api_key: str | None = Field(default=None, validation_alias="AMPLITUDE_API_KEY")
    posthog_api_key: str | None = Field(default=None, validation_alias="POSTHOG_API_KEY")
    flush_interval_seconds: int = Field(default=30, validation_alias="ANALYTICS_FLUSH_INTERVAL")
    buffer_key: str = "analytics:buffer"


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV)

    sentry_dsn: str | None = Field(default=None, validation_alias="SENTRY_DSN")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


class Settings(BaseSettings):
    model_config = _ENV

    debug: bool = Field(default=False, validation_alias="DEBUG")

    bot: BotSettings = Field(default_factory=BotSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Three things to notice. `AMPLITUDE_API_KEY` is now optional, fixing the import-time hard requirement. Secrets are `SecretStr` so they cannot leak into a log line or a `repr`. There is no module-level `settings` singleton — everything goes through `get_settings()`, which the DI container calls exactly once.

- [ ] **Step 2: Add the new optional variables to `.env.example`**

Append to the existing file, keeping every current entry:

```bash
# Logging / analytics tuning (optional)
LOG_LEVEL="INFO"
ANALYTICS_FLUSH_INTERVAL=30
```

- [ ] **Step 3: Verify env names still resolve**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
from bot.core.config import get_settings
s = get_settings()
print('token set   :', bool(s.bot.token.get_secret_value()))
print('db.url      :', s.db.url)
print('redis.url   :', s.redis.url)
print('webhook.url :', s.webhook.url)
print('amplitude   :', s.analytics.amplitude_api_key)
print('repr leaks? :', 'AAHdq' in repr(s))
"
```

Expected: `token set: True`; `db.url` containing `localhost:55432`; `amplitude: None` (proving it is optional now); `repr leaks?: False`.

- [ ] **Step 4: Confirm the old import surface is gone**

`bot/core/config.py` no longer exports `settings`. Other modules still import it and will fail until Task 4 rewires them. That is expected — do not fix them here. Confirm the blast radius:

```bash
grep -rn "from bot.core.config import" bot/ admin/ migrations/ | grep -v "get_settings\|DIR\|LOCALES_DIR\|I18N_DOMAIN\|DEFAULT_LOCALE"
```

Record the list in the commit message; Task 4 clears it.

- [ ] **Step 5: Commit**

```bash
git add bot/core/config.py .env.example
git commit -m "refactor(config): nest settings by concern, make analytics optional"
```

---

### Task 3: Logging and correlation IDs

**Files:**
- Create: `bot/core/logging.py`

**Interfaces:**
- Consumes: `get_settings()` from Task 2.
- Produces: `setup_logging(settings: Settings) -> None`; `correlation_id: ContextVar[str | None]`; `bind_correlation_id(value: str) -> None`. Tasks 8, 11, and 12 bind and read the correlation id.

- [ ] **Step 1: Create `bot/core/logging.py`**

```python
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from bot.core.config import Settings

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def bind_correlation_id(value: str) -> None:
    """Bind an identifier that every later log line in this task will carry."""
    correlation_id.set(value)


def _patch(record: dict[str, Any]) -> None:
    record["extra"]["correlation_id"] = correlation_id.get() or "-"


class InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn, sqlalchemy, taskiq) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(settings: Settings) -> None:
    logger.remove()
    logger.configure(patcher=_patch)
    logger.add(
        sys.stdout,
        level=settings.observability.log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "cid={extra[correlation_id]} | {name}:{function}:{line} | {message}"
        ),
        backtrace=settings.debug,
        diagnose=settings.debug,
    )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "taskiq", "aiogram"):
        stdlib = logging.getLogger(name)
        stdlib.handlers = [InterceptHandler()]
        stdlib.propagate = False
```

Two deliberate choices. `diagnose` is tied to `debug` because loguru's diagnose mode prints variable values into tracebacks, which would leak tokens in production. The file sink from the old `__main__.py` is dropped: container logs belong on stdout, and rotating a file inside a replica loses data on restart.

- [ ] **Step 2: Verify correlation IDs propagate**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
from bot.core.config import get_settings
from bot.core.logging import setup_logging, bind_correlation_id
from loguru import logger
setup_logging(get_settings())
logger.info('before binding')
bind_correlation_id('update-4242')
logger.info('after binding')
import logging; logging.getLogger('uvicorn').warning('via stdlib bridge')
"
```

Expected: three lines. The first shows `cid=-`, the second `cid=update-4242`, the third also `cid=update-4242` and proves the stdlib bridge works.

- [ ] **Step 3: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check .
git add bot/core/logging.py && git commit -m "feat(core): structured logging with correlation ids and stdlib bridge"
```

---

### Task 4: DI container and Bot factory

**Files:**
- Create: `bot/core/di.py`
- Create: `bot/telegram/__init__.py`, `bot/telegram/factory.py`

**Interfaces:**
- Consumes: `get_settings()` (Task 2), `setup_logging()` (Task 3).
- Produces: `create_bot(settings: Settings) -> Bot`; `AppProvider`; `create_container(settings: Settings) -> AsyncContainer`. APP-scope resolvable types: `Settings`, `Redis`, `AsyncEngine`, `async_sessionmaker[AsyncSession]`, `Bot`, `I18n`, `RedisStorage`. Task 8 adds `RequestProvider`; Task 5 adds `CacheService`; Task 6 adds `UserRepository`.

This task is purely additive. `bot/core/loader.py` still exists and still works; Task 8 removes it.

- [ ] **Step 1: Create the Bot factory**

`bot/telegram/__init__.py` is empty. `bot/telegram/factory.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

if TYPE_CHECKING:
    from bot.core.config import Settings


def create_bot(settings: Settings) -> Bot:
    """Build the Bot. Task 15 attaches the outbound rate limiter here."""
    return Bot(
        token=settings.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
```

- [ ] **Step 2: Create the composition root**

`bot/core/di.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterable

from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n.core import I18n
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide
from redis.asyncio import ConnectionPool, Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.core.config import DEFAULT_LOCALE, I18N_DOMAIN, LOCALES_DIR, Settings
from bot.telegram.factory import create_bot

if TYPE_CHECKING:
    from aiogram import Bot


class AppProvider(Provider):
    """Process-lifetime objects. Built once, torn down on shutdown."""

    scope = Scope.APP

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    @provide
    def settings(self) -> Settings:
        return self._settings

    @provide
    async def redis(self, settings: Settings) -> AsyncIterable[Redis]:
        pool = ConnectionPool.from_url(settings.redis.url)
        client = Redis(connection_pool=pool)
        yield client
        await client.aclose()
        await pool.aclose()

    @provide
    async def engine(self, settings: Settings) -> AsyncIterable[AsyncEngine]:
        engine = create_async_engine(
            url=settings.db.url,
            echo=settings.debug,
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.max_overflow,
            pool_pre_ping=True,
            # pgbouncer runs in transaction pooling mode, where server-side
            # prepared statements cannot be reused across transactions. Both of
            # these are DBAPI-level arguments and must sit inside connect_args --
            # `prepared_statement_cache_size` is NOT a valid top-level
            # create_async_engine() kwarg and raises TypeError there.
            connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
        )
        yield engine
        await engine.dispose()

    @provide
    def sessionmaker(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @provide
    async def bot(self, settings: Settings) -> AsyncIterable[Bot]:
        bot = create_bot(settings)
        yield bot
        await bot.session.close()

    @provide
    async def storage(self, redis: Redis) -> AsyncIterable[RedisStorage]:
        storage = RedisStorage(redis=redis, key_builder=DefaultKeyBuilder(with_bot_id=True))
        yield storage
        await storage.close()

    @provide
    def i18n(self) -> I18n:
        return I18n(path=LOCALES_DIR, default_locale=DEFAULT_LOCALE, domain=I18N_DOMAIN)


def create_container(settings: Settings) -> AsyncContainer:
    return make_async_container(AppProvider(settings))
```

The `connect_args` and `prepared_statement_cache_size` replace the `CConnection` UUID hack in `bot/database/database.py`. That hack exists only to survive pgbouncer's *session* pooling; disabling the caches is what transaction pooling actually requires, and it removes the custom connection class entirely. Task 8 deletes that file.

- [ ] **Step 3: Compile the locales**

`I18n(...)` raises at construction if a `.po` has no compiled `.mo`, and `.mo` files are gitignored:

```bash
uv run pybabel compile -d bot/locales
```

- [ ] **Step 4: Verify every APP-scope dependency resolves and closes**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio
from aiogram import Bot
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.i18n.core import I18n
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from bot.core.config import get_settings
from bot.core.di import create_container

async def main():
    c = create_container(get_settings())
    for t in (Redis, AsyncEngine, Bot, RedisStorage, I18n):
        print(f'{t.__name__:16} ->', type(await c.get(t)).__name__)
    sm = await c.get(async_sessionmaker[AsyncSession])
    async with sm() as s:
        print('db query        ->', (await s.execute(text('select 1'))).scalar_one())
    print('redis ping      ->', await (await c.get(Redis)).ping())
    await c.close()
    print('container closed cleanly')

asyncio.run(main())"
```

Expected: each type resolves, `db query -> 1`, `redis ping -> True`, `container closed cleanly`. A hang at close means a generator provider is missing its cleanup half.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/core/di.py bot/telegram/
git commit -m "feat(core): add dishka composition root and bot factory"
```

---

### Task 5: Cache key registry and CacheService

**Files:**
- Create: `bot/cache/keys.py`, `bot/cache/service.py`
- Modify: `bot/core/di.py`

**Interfaces:**
- Consumes: `Redis` from the container (Task 4).
- Produces: `CacheKeys` with `user_exists/user_language/user_first_name/user_is_admin` (each `(user_id: int) -> str`), `user_count() -> str`, and `for_user(user_id: int) -> list[str]`. `CacheService` with `get(key, type_) `, `set(key, value, ttl=None)`, `delete(*keys)`, `invalidate_user(user_id)`. Task 7 consumes all of it.

- [ ] **Step 1: Create the key registry**

The current cache derives keys from `func.__module__:func.__name__`, so a write site cannot know which key a read site produced — that is the root cause of the stale-cache defects. Centralise them. `bot/cache/keys.py`:

```python
from __future__ import annotations

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
```

- [ ] **Step 2: Create `bot/cache/service.py`**

```python
from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import orjson
from loguru import logger

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from redis.asyncio import Redis

T = TypeVar("T", bound=bool | int | float | str | list | dict)

DEFAULT_TTL = 60


class CacheService:
    """Cache-aside helper over Redis using orjson.

    Pickle is deliberately not supported: values are deserialized from Redis,
    and `pickle.loads` on attacker-controlled bytes is remote code execution.
    Only JSON-representable values may be cached.
    """

    def __init__(self, redis: Redis, default_ttl: int = DEFAULT_TTL) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    async def get(self, key: str, type_: type[T]) -> T | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            value = orjson.loads(raw)
        except orjson.JSONDecodeError:
            logger.warning(f"discarding undecodable cache entry | key: {key}")
            await self._redis.delete(key)
            return None
        if not isinstance(value, type_):
            logger.warning(f"cache type mismatch | key: {key} | want: {type_.__name__}")
            await self._redis.delete(key)
            return None
        return value

    async def set(self, key: str, value: T, ttl: int | None = None) -> None:
        await self._redis.set(key, orjson.dumps(value), ex=ttl or self._default_ttl)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._redis.delete(*keys)

    async def invalidate_user(self, user_id: int) -> None:
        await self.delete(*CacheKeys.for_user(user_id), CacheKeys.user_count())
```

`invalidate_user` deletes an explicit list. Never use `KEYS` for invalidation — it is O(N) over the whole keyspace and blocks Redis for every other client.

- [ ] **Step 3: Register `CacheService` in the container**

In `bot/core/di.py`, add the import and the provider inside `AppProvider`:

```python
from bot.cache.service import CacheService
```

```python
    @provide
    def cache(self, redis: Redis) -> CacheService:
        return CacheService(redis)
```

- [ ] **Step 4: Verify round-trip, invalidation, and the pickle rejection**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio, pickle
from redis.asyncio import Redis
from bot.cache.keys import CacheKeys
from bot.cache.service import CacheService
from bot.core.config import get_settings
from bot.core.di import create_container

async def main():
    c = create_container(get_settings())
    cache = await c.get(CacheService); redis = await c.get(Redis)
    k = CacheKeys.user_language(777)
    print('key format   :', k)
    await cache.set(k, 'uk', ttl=30)
    print('round trip   :', await cache.get(k, str))
    print('wrong type   :', await cache.get(k, int), '(None + key dropped)')
    await cache.set(k, 'uk', ttl=30)
    await cache.invalidate_user(777)
    print('after inval  :', await cache.get(k, str))
    await redis.set(k, pickle.dumps({'evil': True}))
    print('pickle bytes :', await cache.get(k, dict), '(rejected, not executed)')
    await c.close()

asyncio.run(main())"
```

Expected: `key format: tpl:v1:user:777:language`; `round trip: uk`; `wrong type: None`; `after inval: None`; `pickle bytes: None`. The last line is the security assertion — pickled bytes are discarded rather than deserialized.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/cache/keys.py bot/cache/service.py bot/core/di.py
git commit -m "feat(cache): add versioned key registry and orjson cache service"
```

---

### Task 6: UserRepository and bounded-memory export

**Files:**
- Create: `bot/database/repositories/__init__.py`, `bot/database/repositories/user.py`
- Rewrite: `bot/utils/users_export.py`

**Interfaces:**
- Consumes: `UserModel` from `bot/database/models`.
- Produces: `UserRepository(session: AsyncSession)` with `get(user_id) -> UserModel | None`, `exists(user_id) -> bool`, `create(user: TgUser, referrer: str | None) -> UserModel`, `language_of(user_id) -> str | None`, `first_name_of(user_id) -> str | None`, `is_admin(user_id) -> bool`, `set_language(user_id, code) -> None`, `set_admin(user_id, value) -> None`, `set_blocked(user_id, value) -> None`, `count() -> int`, `stream(batch_size=1000) -> AsyncIterator[UserModel]`. Also `stream_users_csv(users) -> AsyncIterator[bytes]` and `csv_filename() -> str`. Tasks 7, 8, and 14 consume these.

- [ ] **Step 1: Create `bot/database/repositories/user.py`**

```python
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator

from sqlalchemy import func, select, update

from bot.database.models import UserModel

if TYPE_CHECKING:
    from aiogram.types import User as TgUser
    from sqlalchemy.ext.asyncio import AsyncSession


class UserRepository:
    """All access to the `users` table. No caching here — that is the service layer."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> UserModel | None:
        return await self._session.get(UserModel, user_id)

    async def exists(self, user_id: int) -> bool:
        query = select(UserModel.id).filter_by(id=user_id).limit(1)
        return (await self._session.execute(query)).scalar_one_or_none() is not None

    async def create(self, user: TgUser, referrer: str | None) -> UserModel:
        new_user = UserModel(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            language_code=user.language_code,
            is_premium=user.is_premium or False,
            referrer=referrer,
        )
        self._session.add(new_user)
        await self._session.commit()
        return new_user

    async def language_of(self, user_id: int) -> str | None:
        query = select(UserModel.language_code).filter_by(id=user_id)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def first_name_of(self, user_id: int) -> str | None:
        query = select(UserModel.first_name).filter_by(id=user_id)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def is_admin(self, user_id: int) -> bool:
        query = select(UserModel.is_admin).filter_by(id=user_id)
        return bool((await self._session.execute(query)).scalar_one_or_none())

    async def set_language(self, user_id: int, language_code: str) -> None:
        await self._update(user_id, language_code=language_code)

    async def set_admin(self, user_id: int, *, value: bool) -> None:
        await self._update(user_id, is_admin=value)

    async def set_blocked(self, user_id: int, *, value: bool) -> None:
        await self._update(user_id, is_block=value)

    async def _update(self, user_id: int, **values: object) -> None:
        await self._session.execute(update(UserModel).where(UserModel.id == user_id).values(**values))
        await self._session.commit()

    async def count(self) -> int:
        query = select(func.count()).select_from(UserModel)
        return int((await self._session.execute(query)).scalar_one_or_none() or 0)

    async def stream(self, batch_size: int = 1000) -> AsyncIterator[UserModel]:
        """Yield every user using keyset pagination.

        Replaces `get_all_users()`, which selected the whole table into a list.
        Memory stays bounded by `batch_size` regardless of table size, and keyset
        paging does not degrade on deep pages the way OFFSET does.
        """
        last_id = 0
        while True:
            query = (
                select(UserModel).where(UserModel.id > last_id).order_by(UserModel.id).limit(batch_size)
            )
            rows = (await self._session.execute(query)).scalars().all()
            if not rows:
                return
            for row in rows:
                yield row
            last_id = rows[-1].id
```

`bot/database/repositories/__init__.py`:

```python
from .user import UserRepository

__all__ = ["UserRepository"]
```

- [ ] **Step 2: Rewrite `bot/utils/users_export.py` to stream**

The current version builds the entire CSV in a `StringIO` from a fully materialized list. Replace the file:

```python
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator

from bot.database.models import UserModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

COLUMNS = [column.name for column in UserModel.__table__.columns]


def csv_filename() -> str:
    return f"users_{datetime.now(timezone.utc).strftime('%Y.%m.%d_%H.%M')}.csv"


async def stream_users_csv(users: AsyncIterable[UserModel], chunk_rows: int = 500) -> AsyncIterator[bytes]:
    """Yield CSV bytes in chunks, never holding the full table in memory."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    rows = 0

    async for user in users:
        writer.writerow([getattr(user, column) for column in COLUMNS])
        rows += 1
        if rows % chunk_rows == 0:
            yield buffer.getvalue().encode()
            buffer.seek(0)
            buffer.truncate(0)

    tail = buffer.getvalue()
    if tail:
        yield tail.encode()
```

- [ ] **Step 3: Register the repository in the container**

In `bot/core/di.py`, add a REQUEST-scope provider. Add imports:

```python
from bot.database.repositories import UserRepository
```

and a new provider class below `AppProvider`:

```python
class RequestProvider(Provider):
    """Per-update objects. The session is created only if something resolves it."""

    scope = Scope.REQUEST

    @provide
    async def session(self, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterable[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    @provide
    def users(self, session: AsyncSession) -> UserRepository:
        return UserRepository(session)
```

Update `create_container` to include it:

```python
def create_container(settings: Settings) -> AsyncContainer:
    return make_async_container(AppProvider(settings), RequestProvider())
```

- [ ] **Step 4: Verify against a real table**

```bash
eval "$(scripts/devstack up)"
uv run alembic upgrade head
uv run python -c "
import asyncio
from dishka import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from aiogram.types import User as TgUser
from bot.core.config import get_settings
from bot.core.di import create_container
from bot.database.repositories import UserRepository
from bot.utils.users_export import stream_users_csv, csv_filename

async def main():
    c = create_container(get_settings())
    async with c(scope=Scope.REQUEST) as rc:
        repo = await rc.get(UserRepository)
        for i in range(1, 2501):
            if not await repo.exists(i):
                await repo.create(TgUser(id=i, is_bot=False, first_name=f'U{i}'), referrer=None)
        print('count        :', await repo.count())
        seen = 0
        async for _ in repo.stream(batch_size=100):
            seen += 1
        print('streamed     :', seen)
        chunks = [x async for x in stream_users_csv(repo.stream(batch_size=100))]
        print('csv chunks   :', len(chunks), '| bytes:', sum(len(x) for x in chunks))
        print('filename     :', csv_filename())
        await repo.set_language(1, 'uk')
        print('set_language :', await repo.language_of(1))
    await c.close()

asyncio.run(main())"
```

Expected: `count: 2500`, `streamed: 2500`, `csv chunks` greater than 1 (proving chunking rather than one blob), and `set_language: uk`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/database/repositories/ bot/utils/users_export.py bot/core/di.py
git commit -m "feat(db): add UserRepository with keyset streaming and chunked CSV export"
```

---

### Task 7: UserService — cache invalidation as a structural property

**Files:**
- Rewrite: `bot/services/users.py`
- Modify: `bot/core/di.py`

**Interfaces:**
- Consumes: `UserRepository` (Task 6), `CacheService` + `CacheKeys` (Task 5).
- Produces: `UserService(users: UserRepository, cache: CacheService)` with `ensure_registered(tg_user, referrer) -> bool`, `language_of(user_id) -> str`, `set_language(user_id, code) -> None`, `is_admin(user_id) -> bool`, `set_admin(user_id, value) -> None`, `first_name_of(user_id) -> str`, `count() -> int`, `mark_blocked(user_id, value) -> None`. Tasks 8 and 14 consume it.

This task fixes defect 1. In the current code `set_language_code()` and `set_is_admin()` write to Postgres but never call `clear_cache()`, so a read within the 10-second TTL returns the old value. Here every writer invalidates before returning, and it is the only path to a write.

- [ ] **Step 1: Replace `bot/services/users.py` entirely**

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from aiogram.types import User as TgUser

    from bot.cache.service import CacheService
    from bot.database.repositories import UserRepository

USER_TTL = 300
COUNT_TTL = 60


class UserService:
    """Use cases over users. Owns cache invalidation so call sites cannot forget it."""

    def __init__(self, users: UserRepository, cache: CacheService) -> None:
        self._users = users
        self._cache = cache

    async def ensure_registered(self, tg_user: TgUser, referrer: str | None) -> bool:
        """Register the user if new. Returns True when a row was created."""
        key = CacheKeys.user_exists(tg_user.id)
        if await self._cache.get(key, bool):
            return False
        if await self._users.exists(tg_user.id):
            await self._cache.set(key, value=True, ttl=USER_TTL)
            return False

        await self._users.create(tg_user, referrer)
        await self._cache.invalidate_user(tg_user.id)
        await self._cache.set(key, value=True, ttl=USER_TTL)
        return True

    async def language_of(self, user_id: int) -> str:
        key = CacheKeys.user_language(user_id)
        cached = await self._cache.get(key, str)
        if cached is not None:
            return cached
        value = await self._users.language_of(user_id) or ""
        await self._cache.set(key, value, ttl=USER_TTL)
        return value

    async def set_language(self, user_id: int, language_code: str) -> None:
        await self._users.set_language(user_id, language_code)
        await self._cache.delete(CacheKeys.user_language(user_id))

    async def first_name_of(self, user_id: int) -> str:
        key = CacheKeys.user_first_name(user_id)
        cached = await self._cache.get(key, str)
        if cached is not None:
            return cached
        value = await self._users.first_name_of(user_id) or ""
        await self._cache.set(key, value, ttl=USER_TTL)
        return value

    async def is_admin(self, user_id: int) -> bool:
        key = CacheKeys.user_is_admin(user_id)
        cached = await self._cache.get(key, bool)
        if cached is not None:
            return cached
        value = await self._users.is_admin(user_id)
        await self._cache.set(key, value, ttl=USER_TTL)
        return value

    async def set_admin(self, user_id: int, *, value: bool) -> None:
        await self._users.set_admin(user_id, value=value)
        await self._cache.delete(CacheKeys.user_is_admin(user_id))

    async def mark_blocked(self, user_id: int, *, value: bool) -> None:
        await self._users.set_blocked(user_id, value=value)
        await self._cache.invalidate_user(user_id)

    async def count(self) -> int:
        key = CacheKeys.user_count()
        cached = await self._cache.get(key, int)
        if cached is not None:
            return cached
        value = await self._users.count()
        await self._cache.set(key, value, ttl=COUNT_TTL)
        return value
```

- [ ] **Step 2: Register in the container**

In `bot/core/di.py`, import and add to `RequestProvider`:

```python
from bot.services.users import UserService
```

```python
    @provide
    def user_service(self, users: UserRepository, cache: CacheService) -> UserService:
        return UserService(users, cache)
```

- [ ] **Step 3: Verify the stale-read defect is actually gone**

This check reproduces the current bug and asserts it no longer happens.

```bash
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio
from aiogram.types import User as TgUser
from dishka import Scope
from bot.core.config import get_settings
from bot.core.di import create_container
from bot.services.users import UserService

async def main():
    c = create_container(get_settings())
    uid = 990001
    async with c(scope=Scope.REQUEST) as rc:
        svc = await rc.get(UserService)
        created = await svc.ensure_registered(
            TgUser(id=uid, is_bot=False, first_name='Ada', language_code='ru'), referrer='promo')
        print('created         :', created)
        print('idempotent      :', await svc.ensure_registered(
            TgUser(id=uid, is_bot=False, first_name='Ada'), referrer=None), '(False expected)')
        print('language        :', await svc.language_of(uid))
        await svc.set_language(uid, 'uk')
        print('after set_lang  :', await svc.language_of(uid), '(uk expected, NOT ru)')
        print('is_admin        :', await svc.is_admin(uid))
        await svc.set_admin(uid, value=True)
        print('after set_admin :', await svc.is_admin(uid), '(True expected, NOT False)')
        print('count           :', await svc.count())
    await c.close()

asyncio.run(main())"
```

Expected: `after set_lang: uk` and `after set_admin: True`. Against the old `bot/services/users.py` both of these return the stale value for 10 seconds. If either is stale, the invalidation call is missing.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/services/users.py bot/core/di.py
git commit -m "fix(services): make cache invalidation structural, fixing stale reads after writes"
```

---

### Task 8: Cutover — delete every import-time global

**Files:**
- Create: `bot/core/lifespan.py`, `bot/entrypoints/__init__.py`, `bot/entrypoints/polling.py`, `bot/middlewares/types.py`, `bot/middlewares/analytics.py`
- Modify: `bot/analytics/types.py`, `bot/middlewares/{__init__,auth,i18n,throttling}.py`, `bot/filters/admin.py`, `bot/handlers/{start,export_users}.py`, `bot/__main__.py`, `migrations/env.py`, `bot/core/di.py`
- Delete: `bot/core/loader.py`, `bot/database/database.py`, `bot/middlewares/database.py`, `bot/cache/redis.py`, `bot/cache/serialization.py`, `bot/utils/singleton.py`, `bot/services/analytics.py`

**Interfaces:**
- Consumes: everything from Tasks 2-7.
- Produces: `AppContext` (dataclass with `.container`, `.bot`, `.dp`); `build_dispatcher(container) -> Dispatcher`; `lifespan(settings) -> AsyncIterator[AppContext]`; `register_middlewares(dp, container) -> None`; `NullAnalyticsLogger`. Task 12 reuses `lifespan` and `build_dispatcher` for the API process.

This is the task that makes the codebase testable and multi-replica capable. It is large because the cutover has to be atomic: `bot/core/loader.py` is imported by the middlewares and the cache, so it cannot be removed piecemeal.

- [ ] **Step 1: Add a null analytics logger and stop analytics blocking handlers**

Append to `bot/analytics/types.py`:

```python
class NullAnalyticsLogger(AbstractAnalyticsLogger):
    """Used when no analytics provider is configured."""

    async def log_event(self, event: BaseEvent) -> None:
        return None
```

Create `bot/middlewares/analytics.py`. This replaces the `@analytics.track_event(...)` decorator, which required a module-level singleton to exist at import time and — worse — awaited the Amplitude POST *before* invoking the handler, so a provider outage stopped handlers running (defect 5):

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message
from loguru import logger

from bot.analytics.types import BaseEvent, EventProperties, UserProperties

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

    from bot.analytics.types import AbstractAnalyticsLogger


class AnalyticsMiddleware(BaseMiddleware):
    """Tracks handlers marked with `@flags.analytics_event("Name")`.

    The handler runs first and its result is returned regardless of whether
    tracking succeeds, so the analytics provider can never break the bot.
    """

    def __init__(self, gateway: AbstractAnalyticsLogger) -> None:
        self._gateway = gateway
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        event_name = get_flag(data, "analytics_event")
        result = await handler(event, data)
        if event_name and isinstance(event, Message | CallbackQuery) and event.from_user:
            try:
                await self._gateway.log_event(self._build(event, event_name))
            except Exception as exc:  # noqa: BLE001 - analytics must never break a handler
                logger.warning(f"analytics tracking failed | event: {event_name} | error: {exc}")
        return result

    def _build(self, event: Message | CallbackQuery, event_name: str) -> BaseEvent:
        user = event.from_user
        if isinstance(event, Message):
            chat_id, chat_type, text = event.chat.id, event.chat.type, event.text
            command = event.text if event.text and event.text.startswith("/") else None
        else:
            message = event.message
            chat_id = message.chat.id if message else None
            chat_type = message.chat.type if message else None
            text, command = event.data, None
        return BaseEvent(
            user_id=user.id,
            event_type=event_name,  # type: ignore[arg-type]
            user_properties=UserProperties(
                first_name=user.first_name, last_name=user.last_name,
                username=user.username, url=user.url,
            ),
            event_properties=EventProperties(
                chat_id=chat_id, chat_type=chat_type, text=text, command=command,
            ),
            language=user.language_code,
        )
```

Delete `bot/services/analytics.py` and `bot/utils/singleton.py` — the singleton metaclass existed only for `AnalyticsService`.

- [ ] **Step 2: Type the middleware context**

Create `bot/middlewares/types.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.dispatcher.middlewares.data import MiddlewareData as AiogramMiddlewareData

if TYPE_CHECKING:
    from bot.services.users import UserService


class MiddlewareData(AiogramMiddlewareData, total=False):
    """Context keys this project adds on top of aiogram's own."""

    user_service: UserService
```

- [ ] **Step 3: Rewrite the middlewares that used globals**

`bot/middlewares/auth.py` — resolve `UserService` from the dishka request container instead of importing free functions:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import Message
from loguru import logger

from bot.services.users import UserService
from bot.utils.command import find_command_argument

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject
    from dishka import AsyncContainer


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        container: AsyncContainer = data["dishka_container"]
        users = await container.get(UserService)
        referrer = find_command_argument(event.text)

        if await users.ensure_registered(event.from_user, referrer):
            logger.info(f"new user registration | user_id: {event.from_user.id} | referrer: {referrer}")

        data["user_service"] = users
        return await handler(event, data)
```

`bot/middlewares/i18n.py` — replace the body of `get_locale` (keep the module docstring):

```python
    async def get_locale(self, event: TelegramObject, data: dict[str, Any]) -> str:
        user: User | None = getattr(event, "from_user", None)
        if not user:
            return self.DEFAULT_LANGUAGE_CODE

        container: AsyncContainer = data["dishka_container"]
        users = await container.get(UserService)
        return await users.language_of(user.id) or self.DEFAULT_LANGUAGE_CODE
```

with imports `from bot.services.users import UserService` and, under `TYPE_CHECKING`, `from dishka import AsyncContainer`. Delete the `hasattr(event, "chat_member")` guard: `I18nMiddleware.setup()` attaches to every observer *except* `update`, and only `Update` has a `chat_member` field, so that branch is unreachable (see spec section 1).

`bot/filters/admin.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram.filters import BaseFilter
from aiogram.types import Message

from bot.services.users import UserService

if TYPE_CHECKING:
    from dishka import AsyncContainer


class AdminFilter(BaseFilter):
    """Allows only administrators (whose database column is_admin=True)."""

    async def __call__(self, message: Message, **data: Any) -> bool:
        if not message.from_user:
            return False
        container: AsyncContainer = data["dishka_container"]
        users = await container.get(UserService)
        return await users.is_admin(message.from_user.id)
```

Delete `bot/middlewares/database.py`. The session now comes from `RequestProvider` and is created lazily.

- [ ] **Step 4: Rewrite middleware registration**

`bot/middlewares/__init__.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.utils.callback_answer import CallbackAnswerMiddleware
from aiogram.utils.chat_action import ChatActionMiddleware
from aiogram.utils.i18n.core import I18n

from bot.analytics.types import AbstractAnalyticsLogger

if TYPE_CHECKING:
    from aiogram import Dispatcher
    from dishka import AsyncContainer


async def register_middlewares(dp: Dispatcher, container: AsyncContainer) -> None:
    """Register the update pipeline. Order is load-bearing — see spec section 7."""
    from .analytics import AnalyticsMiddleware
    from .auth import AuthMiddleware
    from .i18n import ACLMiddleware
    from .logging import LoggingMiddleware
    from .throttling import ThrottlingMiddleware

    i18n = await container.get(I18n)
    gateway = await container.get(AbstractAnalyticsLogger)

    dp.update.outer_middleware(LoggingMiddleware())
    dp.message.outer_middleware(ThrottlingMiddleware(settings.bot.rate_limit))

    dp.message.middleware(AuthMiddleware())
    ACLMiddleware(i18n=i18n).setup(dp)
    dp.message.middleware(AnalyticsMiddleware(gateway))
    dp.callback_query.middleware(AnalyticsMiddleware(gateway))
    dp.message.middleware(ChatActionMiddleware())
    dp.callback_query.middleware(CallbackAnswerMiddleware())
```

Also resolve `Settings` at the top of the function, next to `i18n` and `gateway`:

```python
    settings = await container.get(Settings)
```

with `from bot.core.config import Settings`.

The existing `ThrottlingMiddleware` defaults its rate limit from the module-level
`settings` object that Task 2 deleted, so it needs a one-line change now — Task 10
replaces the class body entirely. In `bot/middlewares/throttling.py`, make the
parameter required and drop the `bot.core.config` import:

```python
    def __init__(self, rate_limit: float) -> None:
        self.cache = TTLCache(maxsize=10_000, ttl=rate_limit)
```

This keeps throttling working (still in-process, still wrong for multi-replica)
until Task 10 makes it Redis-backed.

- [ ] **Step 5: Create the shared lifespan**

`bot/core/lifespan.py`:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from dishka.integrations.aiogram import setup_dishka
from loguru import logger
from sentry_sdk.integrations.loguru import LoggingLevels, LoguruIntegration

from bot.core.di import create_container
from bot.core.logging import setup_logging
from bot.handlers import get_handlers_router
from bot.middlewares import register_middlewares

if TYPE_CHECKING:
    from dishka import AsyncContainer

    from bot.core.config import Settings


@dataclass(slots=True)
class AppContext:
    container: AsyncContainer
    bot: Bot
    dp: Dispatcher


def _setup_sentry(settings: Settings) -> None:
    if not settings.observability.sentry_dsn:
        return
    sentry_sdk.init(
        dsn=settings.observability.sentry_dsn,
        enable_tracing=True,
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0,
        integrations=[
            LoguruIntegration(level=LoggingLevels.INFO.value, event_level=LoggingLevels.ERROR.value),
        ],
    )


async def build_dispatcher(container: AsyncContainer) -> Dispatcher:
    storage = await container.get(RedisStorage)
    dp = Dispatcher(storage=storage)
    setup_dishka(container=container, router=dp, auto_inject=True)
    await register_middlewares(dp, container)
    dp.include_router(get_handlers_router())
    return dp


@asynccontextmanager
async def lifespan(settings: Settings) -> AsyncIterator[AppContext]:
    """Startup and shutdown shared by every entrypoint."""
    setup_logging(settings)
    _setup_sentry(settings)

    container = create_container(settings)
    bot = await container.get(Bot)
    dp = await build_dispatcher(container)

    info = await bot.get_me()
    logger.info(f"bot started | @{info.username} | id: {info.id}")
    try:
        yield AppContext(container=container, bot=bot, dp=dp)
    finally:
        logger.info("shutting down")
        await container.close()
        logger.info("shutdown complete")
```

Sentry's `event_level` is raised from `INFO` to `ERROR`. The current setting sends an event for every info log, which floods the issue stream and burns quota.

- [ ] **Step 6: Create the polling entrypoint**

`bot/entrypoints/__init__.py` is empty. `bot/entrypoints/polling.py`:

```python
from __future__ import annotations

import asyncio

import uvloop

from bot.core.config import get_settings
from bot.core.lifespan import lifespan
from bot.keyboards.default_commands import remove_default_commands, set_default_commands


async def main() -> None:
    """Development entrypoint. Telegram allows only one polling process per token,
    so this is never a production path — use `bot.entrypoints.api` instead.
    """
    settings = get_settings()
    async with lifespan(settings) as ctx:
        await ctx.bot.delete_webhook(drop_pending_updates=False)
        await set_default_commands(ctx.bot)
        try:
            await ctx.dp.start_polling(ctx.bot, allowed_updates=ctx.dp.resolve_used_update_types())
        finally:
            await remove_default_commands(ctx.bot)


def run() -> None:
    uvloop.run(main())


if __name__ == "__main__":
    run()
```

Replace `bot/__main__.py` entirely:

```python
from bot.entrypoints.polling import run

if __name__ == "__main__":
    run()
```

- [ ] **Step 7: Convert the analytics decorator to a flag**

In `bot/handlers/start.py`, replace the `@analytics.track_event("Sign Up")` decorator and its import:

```python
from aiogram import Router, flags, types
from aiogram.filters import CommandStart
from aiogram.utils.i18n import gettext as _

from bot.keyboards.inline.menu import main_keyboard

router = Router(name="start")


@router.message(CommandStart())
@flags.analytics_event("Sign Up")
async def start_handler(message: types.Message) -> None:
    """Welcome message."""
    await message.answer(_("first message"), reply_markup=main_keyboard())
```

In `bot/handlers/export_users.py`, replace the `get_all_users` / `get_user_count` imports with injected dependencies:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.filters.admin import AdminFilter
from bot.utils.users_export import csv_filename, stream_users_csv

if TYPE_CHECKING:
    from aiogram.types import Message

    from bot.database.repositories import UserRepository
    from bot.services.users import UserService

router = Router(name="export_users")


@router.message(Command(commands="export_users"), AdminFilter())
async def export_users_handler(
    message: Message,
    users: FromDishka[UserRepository],
    user_service: FromDishka[UserService],
) -> None:
    """Export all users as CSV. Task 14 moves this to a background task."""
    chunks = [chunk async for chunk in stream_users_csv(users.stream())]
    document = BufferedInputFile(file=b"".join(chunks), filename=csv_filename())
    count = await user_service.count()
    await message.answer_document(document=document, caption=_("user counter: <b>{count}</b>").format(count=count))
```

- [ ] **Step 8: Fix the Alembic environment**

`migrations/env.py` line 11 imports the deleted `settings` singleton. Replace that import and the line that uses it:

```python
from bot.core.config import get_settings
```

```python
config.set_main_option("sqlalchemy.url", str(get_settings().db.url))
```

- [ ] **Step 9: Delete the global modules**

```bash
git rm bot/core/loader.py bot/database/database.py bot/middlewares/database.py \
       bot/cache/redis.py bot/cache/serialization.py bot/utils/singleton.py \
       bot/services/analytics.py
```

Register the analytics gateway in `bot/core/di.py` — add to `AppProvider`:

```python
    @provide
    def analytics(self, settings: Settings) -> AbstractAnalyticsLogger:
        if settings.analytics.amplitude_api_key:
            return AmplitudeTelegramLogger(api_token=settings.analytics.amplitude_api_key)
        return NullAnalyticsLogger()
```

with imports `from bot.analytics.amplitude import AmplitudeTelegramLogger` and `from bot.analytics.types import AbstractAnalyticsLogger, NullAnalyticsLogger`.

- [ ] **Step 10: Prove no import-time side effects remain**

```bash
grep -rn "loader\|^settings = \|^engine = \|^sessionmaker = \|^redis_client = \|^bot = Bot(\|^dp = Dispatcher(" bot/ migrations/ || echo "no module-level globals found"
uv run python -c "import bot.handlers, bot.middlewares, bot.core.di; print('imports clean, no connections opened')"
```

The second command must succeed **without** `DB_HOST`/`REDIS_HOST` set — importing the package must no longer touch the network. Run it in a shell where the devstack vars are not exported.

- [ ] **Step 11: Run the bot end to end**

```bash
eval "$(scripts/devstack up)"
uv run alembic upgrade head
uv run pybabel compile -d bot/locales
uv run python -c "
import asyncio, datetime
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User
from bot.core.config import get_settings
from bot.core.lifespan import lifespan

sent = []
async def fake(bot, method, timeout=None):
    if isinstance(method, SendMessage):
        sent.append(method.text)
        return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                       chat=Chat(id=method.chat_id, type='private'), text=method.text)
    return True

async def main():
    async with lifespan(get_settings()) as ctx:
        ctx.bot.session.make_request = fake
        u = User(id=880001, is_bot=False, first_name='Ada', language_code='en')
        c = Chat(id=880001, type='private')
        for i, text in enumerate(['/start ref42', '/info', '/menu'], start=1):
            await ctx.dp.feed_update(ctx.bot, Update(update_id=i, message=Message(
                message_id=i, date=datetime.datetime.now(datetime.timezone.utc),
                chat=c, from_user=u, text=text)))
            await asyncio.sleep(0.6)
        print('replies:', sent)

asyncio.run(main())"
```

Expected: three replies (welcome, about, menu). `bot.get_me()` inside `lifespan` will fail against Telegram with the example token — if it does, temporarily stub it the same way, or set a real `BOT_TOKEN`. Note which you did in the commit message.

- [ ] **Step 12: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python -c "import admin.app" 2>&1 | tail -3   # must fail only on DB connection, not ImportError
git add -A
git commit -m "refactor!: replace import-time globals with a dishka composition root

Deletes bot/core/loader.py, bot/database/database.py, the database middleware,
the pickle-based cache decorator, and the analytics singleton. Handlers and
filters now resolve dependencies from the request container.

Analytics moves from a blocking decorator to a flag-driven middleware that runs
after the handler, so a provider outage can no longer stop handlers running."
```

The `admin.app` check matters: the admin panel must still import. A `sqlalchemy.exc.OperationalError` about connecting to Postgres is fine and expected; an `ImportError` or `ModuleNotFoundError` means the cutover broke the admin panel and must be fixed before committing.

---

### Task 9: Update deduplication

**Files:**
- Create: `bot/middlewares/dedup.py`
- Modify: `bot/middlewares/__init__.py`, `bot/cache/keys.py`

**Interfaces:**
- Consumes: `Redis` from the container.
- Produces: `DedupMiddleware(redis: Redis, ttl: int = 600)`; `CacheKeys.dedup(update_id) -> str`.

Telegram redelivers a webhook update when the response is slow. Behind a load balancer the retry reaches a *different* replica than the original, so an in-process guard cannot catch it — the result is a duplicated `/start`, opt-in, or payment.

- [ ] **Step 1: Add the key**

In `bot/cache/keys.py`, add to `CacheKeys`:

```python
    @classmethod
    def dedup(cls, update_id: int) -> str:
        return cls._key("dedup", update_id)
```

- [ ] **Step 2: Create `bot/middlewares/dedup.py`**

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import Update
from loguru import logger

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject
    from redis.asyncio import Redis

DEDUP_TTL = 600


class DedupMiddleware(BaseMiddleware):
    """Drops updates already claimed by another replica.

    Must be the outermost middleware: anything registered before it does its
    work twice on a redelivered update.
    """

    def __init__(self, redis: Redis, ttl: int = DEDUP_TTL) -> None:
        self._redis = redis
        self._ttl = ttl
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        claimed = await self._redis.set(CacheKeys.dedup(event.update_id), 1, nx=True, ex=self._ttl)
        if not claimed:
            logger.warning(f"duplicate update dropped | update_id: {event.update_id}")
            return None

        return await handler(event, data)
```

`SET ... NX EX` is a single atomic round trip: exactly one replica gets `True`.

- [ ] **Step 3: Register it first**

In `bot/middlewares/__init__.py`, import `Redis` and `DedupMiddleware`, resolve the client, and make dedup the first registration — before `LoggingMiddleware`:

```python
    redis = await container.get(Redis)

    dp.update.outer_middleware(DedupMiddleware(redis))
    dp.update.outer_middleware(LoggingMiddleware())
```

- [ ] **Step 4: Verify a redelivered update is dropped once**

```bash
eval "$(scripts/devstack up)" && uv run alembic upgrade head
uv run python -c "
import asyncio, datetime
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User
from bot.core.config import get_settings
from bot.core.lifespan import lifespan

sent = []
async def fake(bot, method, timeout=None):
    if isinstance(method, SendMessage):
        sent.append(method.text)
        return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                       chat=Chat(id=method.chat_id, type='private'), text=method.text)
    return True

async def main():
    async with lifespan(get_settings()) as ctx:
        ctx.bot.session.make_request = fake
        u = User(id=880002, is_bot=False, first_name='Grace', language_code='en')
        upd = Update(update_id=5150, message=Message(
            message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=880002, type='private'), from_user=u, text='/start'))
        await ctx.dp.feed_update(ctx.bot, upd)
        await ctx.dp.feed_update(ctx.bot, upd)   # same update_id, as Telegram would redeliver
        print('replies sent:', len(sent), '(1 expected)')

asyncio.run(main())"
```

Expected: `replies sent: 1 (1 expected)`, with a `duplicate update dropped` warning in the log.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/middlewares/dedup.py bot/middlewares/__init__.py bot/cache/keys.py
git commit -m "feat(middleware): drop redelivered updates via atomic Redis claim"
```

---

### Task 10: Redis token bucket and distributed throttling

**Files:**
- Create: `bot/cache/ratelimit.py`
- Rewrite: `bot/middlewares/throttling.py`
- Modify: `bot/cache/keys.py`, `bot/middlewares/__init__.py`, `bot/core/di.py`, `pyproject.toml`

**Interfaces:**
- Consumes: `Redis`, `Settings`.
- Produces: `TokenBucket(redis, rate, capacity, name)` with `acquire(key, tokens=1.0) -> float` (seconds to wait; `0.0` means granted) and `try_acquire(key, tokens=1.0) -> bool`; `CacheKeys.throttle(scope, ident) -> str`. Task 15 reuses `TokenBucket` for outbound limiting.

- [ ] **Step 1: Create the token bucket primitive**

`bot/cache/ratelimit.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from redis.commands.core import AsyncScript

# Refill and consume atomically. Redis' own TIME is the clock, so replicas with
# skewed system clocks cannot disagree about how full a bucket is.
_LUA = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)

local wait = 0
if tokens >= requested then
  tokens = tokens - requested
else
  wait = (requested - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil((capacity / rate) * 2000))
return tostring(wait)
"""


class TokenBucket:
    """Distributed token bucket. One bucket per key, shared by every process."""

    def __init__(self, redis: Redis, rate: float, capacity: float, name: str) -> None:
        self._rate = rate
        self._capacity = capacity
        self._name = name
        self._script: AsyncScript = redis.register_script(_LUA)

    async def acquire(self, key: str, tokens: float = 1.0) -> float:
        """Return seconds to wait before `tokens` are available. 0.0 means granted."""
        raw = await self._script(keys=[key], args=[self._rate, self._capacity, tokens])
        return float(raw)

    async def try_acquire(self, key: str, tokens: float = 1.0) -> bool:
        return await self.acquire(key, tokens) == 0.0
```

- [ ] **Step 2: Add the key**

In `bot/cache/keys.py`:

```python
    @classmethod
    def throttle(cls, scope: str, ident: int | str) -> str:
        return cls._key("throttle", scope, ident)
```

- [ ] **Step 3: Rewrite the throttler**

Replace `bot/middlewares/throttling.py`. The current version uses `cachetools.TTLCache` in process memory, so three replicas grant a user three times the intended rate:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from loguru import logger

from bot.cache.keys import CacheKeys

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

    from bot.cache.ratelimit import TokenBucket


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user rate limiting shared across replicas.

    Keyed by user rather than chat, so members of a group do not consume each
    other's allowance.
    """

    def __init__(self, bucket: TokenBucket) -> None:
        self._bucket = bucket
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        if not await self._bucket.try_acquire(CacheKeys.throttle("user", user.id)):
            logger.debug(f"throttled | user_id: {user.id}")
            return None

        return await handler(event, data)
```

- [ ] **Step 4: Provide the bucket and register the middleware**

In `bot/core/di.py`, add to `AppProvider`:

```python
    @provide
    def throttle_bucket(self, redis: Redis, settings: Settings) -> TokenBucket:
        rate = 1.0 / settings.bot.rate_limit if settings.bot.rate_limit > 0 else 1.0
        return TokenBucket(redis, rate=rate, capacity=max(1.0, rate), name="throttle")
```

with `from bot.cache.ratelimit import TokenBucket`. In `bot/middlewares/__init__.py`, resolve it and pass it in, replacing the interim registration from Task 8:

```python
    bucket = await container.get(TokenBucket)
    dp.message.outer_middleware(ThrottlingMiddleware(bucket))
```

- [ ] **Step 5: Drop cachetools**

Nothing uses it now. Remove `"cachetools<6.0.0,>=5.5.1"` (or the current pin) from the `bot` group and `"types-cachetools..."` from `dev` in `pyproject.toml`, then:

```bash
uv lock && uv sync --frozen --all-groups
grep -rn "cachetools" bot/ admin/ pyproject.toml || echo "cachetools fully removed"
```

- [ ] **Step 6: Verify the bucket refills and is shared**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio
from redis.asyncio import Redis
from bot.cache.keys import CacheKeys
from bot.cache.ratelimit import TokenBucket
from bot.core.config import get_settings
from bot.core.di import create_container

async def main():
    c = create_container(get_settings())
    redis = await c.get(Redis)
    key = CacheKeys.throttle('user', 424242)
    await redis.delete(key)
    # Two independent bucket objects = two replicas sharing one Redis.
    a = TokenBucket(redis, rate=2.0, capacity=2.0, name='t')
    b = TokenBucket(redis, rate=2.0, capacity=2.0, name='t')
    print('replica A #1:', await a.try_acquire(key), '(True)')
    print('replica B #2:', await b.try_acquire(key), '(True)')
    print('replica A #3:', await a.try_acquire(key), '(False - shared bucket empty)')
    await asyncio.sleep(1.1)
    print('after refill:', await b.try_acquire(key), '(True)')
    await c.close()

asyncio.run(main())"
```

Expected exactly: `True`, `True`, `False`, `True`. The third line is the assertion that matters — with the old in-memory throttler two separate objects would each have their own cache and both return `True`.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add bot/cache/ratelimit.py bot/middlewares/throttling.py bot/cache/keys.py \
        bot/middlewares/__init__.py bot/core/di.py pyproject.toml uv.lock
git commit -m "feat(middleware): replace in-process throttling with a shared Redis token bucket"
```

---

### Task 11: Metrics and the global error handler

**Files:**
- Create: `bot/middlewares/metrics.py`, `bot/handlers/errors.py`
- Modify: `bot/middlewares/{__init__,dedup,throttling,logging}.py`, `bot/core/lifespan.py`

**Interfaces:**
- Consumes: `prometheus_client`, `bind_correlation_id` (Task 3).
- Produces: `MetricsMiddleware()`; counters `UPDATES`, `UPDATE_DURATION`, `THROTTLED`, `DEDUP_HITS`, `HANDLER_ERRORS`; `register_error_handler(dp) -> None`. Task 12 exposes these at `/metrics`.

- [ ] **Step 1: Create `bot/middlewares/metrics.py`**

```python
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import Update
from prometheus_client import Counter, Histogram

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

PREFIX = "tgbot"

UPDATES = Counter(f"{PREFIX}_updates_total", "Updates processed.", ["event_type", "outcome"])
UPDATE_DURATION = Histogram(f"{PREFIX}_update_duration_seconds", "Update processing time.", ["event_type"])
THROTTLED = Counter(f"{PREFIX}_throttled_total", "Updates dropped by throttling.", ["scope"])
DEDUP_HITS = Counter(f"{PREFIX}_dedup_hits_total", "Updates dropped as duplicates.")
HANDLER_ERRORS = Counter(f"{PREFIX}_handler_errors_total", "Unhandled handler exceptions.", ["exception"])


def _event_type(event: TelegramObject) -> str:
    if isinstance(event, Update):
        return event.event_type
    return type(event).__name__


class MetricsMiddleware(BaseMiddleware):
    """RED metrics per update. Registered on `dp.update`, so it works in polling
    mode too — the old aiohttp-only middleware left polling deployments blind."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        event_type = _event_type(event)
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await handler(event, data)
        except Exception:
            outcome = "error"
            raise
        finally:
            UPDATE_DURATION.labels(event_type=event_type).observe(time.perf_counter() - started)
            UPDATES.labels(event_type=event_type, outcome=outcome).inc()
```

- [ ] **Step 2: Increment the drop counters**

In `bot/middlewares/dedup.py`, add `from bot.middlewares.metrics import DEDUP_HITS` and call `DEDUP_HITS.inc()` immediately before `return None`.

In `bot/middlewares/throttling.py`, add `from bot.middlewares.metrics import THROTTLED` and call `THROTTLED.labels(scope="user").inc()` immediately before `return None`.

- [ ] **Step 3: Bind the correlation id**

In `bot/middlewares/logging.py`, at the top of `__call__`, before any logging:

```python
        if isinstance(event, Update):
            bind_correlation_id(str(event.update_id))
```

with `from aiogram.types import Update` and `from bot.core.logging import bind_correlation_id`.

- [ ] **Step 4: Create the error handler**

There is no `dp.errors` handler today, so unhandled exceptions disappear into aiogram's default logger and the user receives silence. `bot/handlers/errors.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

import sentry_sdk
from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message
from aiogram.utils.i18n import gettext as _
from loguru import logger

from bot.core.logging import correlation_id
from bot.middlewares.metrics import HANDLER_ERRORS

if TYPE_CHECKING:
    from aiogram import Dispatcher
    from aiogram.types import ErrorEvent

router = Router(name="errors")


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    exception = event.exception
    cid = correlation_id.get() or "-"

    if isinstance(exception, TelegramRetryAfter | TelegramForbiddenError):
        # Expected and actionable elsewhere: the rate limiter handles the first,
        # and the broadcast task marks blocked users on the second.
        logger.warning(f"telegram rejected a call | cid: {cid} | {type(exception).__name__}: {exception}")
        return True

    HANDLER_ERRORS.labels(exception=type(exception).__name__).inc()
    logger.opt(exception=exception).error(f"unhandled error | cid: {cid}")
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("correlation_id", cid)
        scope.set_context("update", event.update.model_dump(exclude_none=True, mode="json"))
        sentry_sdk.capture_exception(exception)

    target = event.update.message or event.update.callback_query
    if isinstance(target, Message):
        await target.answer(_("something went wrong"))
    elif isinstance(target, CallbackQuery):
        await target.answer(_("something went wrong"), show_alert=True)
    return True


def register_error_handler(dp: Dispatcher) -> None:
    dp.include_router(router)
```

- [ ] **Step 5: Add the translation string**

```bash
uv run pybabel extract --input-dirs=. -o bot/locales/messages.pot
uv run pybabel update -d bot/locales -i bot/locales/messages.pot
```

Then set `msgstr` for `something went wrong` in all three `bot/locales/*/LC_MESSAGES/messages.po` files (English: `⚠️ Something went wrong. Please try again.`; translate for `ru` and `uk`), and compile:

```bash
uv run pybabel compile -d bot/locales
```

- [ ] **Step 6: Wire both into the dispatcher**

In `bot/middlewares/__init__.py`, register metrics right after logging:

```python
    dp.update.outer_middleware(MetricsMiddleware())
```

In `bot/core/lifespan.py`, inside `build_dispatcher`, after `dp.include_router(get_handlers_router())`:

```python
    register_error_handler(dp)
```

with `from bot.handlers.errors import register_error_handler`. The error router is included **last** so feature routers get first refusal on every update.

- [ ] **Step 7: Verify metrics increment and errors are caught**

```bash
eval "$(scripts/devstack up)" && uv run alembic upgrade head
uv run python -c "
import asyncio, datetime
from aiogram import Router
from aiogram.filters import Command
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User
from prometheus_client import generate_latest
from bot.core.config import get_settings
from bot.core.lifespan import lifespan

sent = []
async def fake(bot, method, timeout=None):
    if isinstance(method, SendMessage):
        sent.append(method.text)
        return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                       chat=Chat(id=method.chat_id, type='private'), text=method.text)
    return True

boom = Router(name='boom')
@boom.message(Command('boom'))
async def explode(message: Message) -> None:
    raise RuntimeError('deliberate test failure')

async def main():
    async with lifespan(get_settings()) as ctx:
        ctx.bot.session.make_request = fake
        ctx.dp.include_router(boom)
        u = User(id=880003, is_bot=False, first_name='Ada', language_code='en')
        c = Chat(id=880003, type='private')
        for i, t in enumerate(['/start', '/boom'], start=6000):
            await ctx.dp.feed_update(ctx.bot, Update(update_id=i, message=Message(
                message_id=i, date=datetime.datetime.now(datetime.timezone.utc),
                chat=c, from_user=u, text=t)))
            await asyncio.sleep(0.6)
        print('replies:', sent)
        for line in generate_latest().decode().splitlines():
            if line.startswith(('tgbot_updates_total', 'tgbot_handler_errors_total')):
                print('  ', line)

asyncio.run(main())"
```

Expected: two replies, the second being the apology string; `tgbot_updates_total{...outcome=\"ok\"}` and `tgbot_handler_errors_total{exception="RuntimeError"} 1.0`. The process must not crash — that is the point of the error handler.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add -A
git commit -m "feat(observability): add RED metrics and a global error handler"
```

---

### Task 12: FastAPI entrypoint — webhook, health, metrics

**Files:**
- Create: `bot/api/__init__.py`, `bot/api/health.py`, `bot/api/metrics.py`, `bot/api/webhook.py`, `bot/entrypoints/api.py`
- Delete: `bot/handlers/metrics.py`
- Modify: `bot/__main__.py` (docstring only)

**Interfaces:**
- Consumes: `lifespan`, `AppContext` (Task 8); metrics registry (Task 11).
- Produces: `app: FastAPI` at `bot.entrypoints.api:app`, serving `POST {webhook.path}`, `GET /health/live`, `GET /health/ready`, `GET /metrics`.

- [ ] **Step 1: Health routes**

`bot/api/health.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness: the process is running. Never touches a dependency — a failing
    database must not get the container killed and restarted in a loop."""
    return {"status": "alive"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, object]:
    """Readiness: this replica can serve traffic. The load balancer uses this."""
    container = request.app.state.ctx.container
    checks: dict[str, bool] = {}

    try:
        sessionmaker = await container.get(async_sessionmaker[AsyncSession])
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001 - readiness reports, never raises
        checks["postgres"] = False

    try:
        checks["redis"] = bool(await (await container.get(Redis)).ping())
    except Exception:  # noqa: BLE001
        checks["redis"] = False

    ok = all(checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ok else "degraded", "checks": checks}
```

Splitting liveness from readiness matters: if they were one endpoint, a brief database blip would make the orchestrator kill healthy replicas instead of merely routing traffic away from them.

- [ ] **Step 2: Metrics route**

`bot/api/metrics.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

Delete `bot/handlers/metrics.py` — the aiohttp view it contained was only mounted in webhook mode.

- [ ] **Step 3: Webhook route**

`bot/api/webhook.py`:

```python
from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request, status
from loguru import logger

if TYPE_CHECKING:
    from bot.core.config import Settings

# Telegram's published webhook source ranges.
TELEGRAM_SUBNETS = (
    ipaddress.ip_network("149.154.160.0/20"),
    ipaddress.ip_network("91.108.4.0/22"),
)

router = APIRouter(tags=["telegram"])


def _from_telegram(client_ip: str) -> bool:
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(address in subnet for subnet in TELEGRAM_SUBNETS)


@router.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict[str, bool]:
    ctx = request.app.state.ctx
    settings: Settings = request.app.state.settings

    if settings.webhook.secret and x_telegram_bot_api_secret_token != settings.webhook.secret:
        logger.warning("webhook rejected | bad secret token")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    if settings.webhook.verify_source_ip:
        client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else ""
        )
        if not _from_telegram(client_ip):
            logger.warning(f"webhook rejected | source ip not Telegram: {client_ip}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    update = Update.model_validate(await request.json(), context={"bot": ctx.bot})
    await ctx.dp.feed_update(ctx.bot, update)
    return {"ok": True}
```

Add the toggle to `WebhookSettings` in `bot/core/config.py`:

```python
    verify_source_ip: bool = Field(default=True, validation_alias="WEBHOOK_VERIFY_SOURCE_IP")
```

and to `.env.example`:

```bash
WEBHOOK_VERIFY_SOURCE_IP=True   # set False when a proxy rewrites the source address
```

The route returns immediately after `feed_update` so Telegram sees a fast 200. Slow work belongs in the TaskIQ tasks from Tasks 13-14, not here.

- [ ] **Step 4: The API entrypoint**

`bot/api/__init__.py` is empty. `bot/entrypoints/api.py`:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from loguru import logger

from bot.api import health, metrics, webhook
from bot.core.config import get_settings
from bot.core.lifespan import lifespan as app_lifespan
from bot.keyboards.default_commands import set_default_commands


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    async with app_lifespan(settings) as ctx:
        app.state.ctx = ctx
        await set_default_commands(ctx.bot)
        if settings.webhook.enabled:
            await ctx.bot.set_webhook(
                settings.webhook.url,
                allowed_updates=ctx.dp.resolve_used_update_types(),
                secret_token=settings.webhook.secret or None,
                drop_pending_updates=False,
            )
            logger.info(f"webhook registered | {settings.webhook.url}")
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="Telegram Bot", lifespan=_lifespan, docs_url=None, redoc_url=None)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(webhook.router)
    return app


app = create_app()
```

`set_webhook` is **not** called with `drop_pending_updates=True`, and `delete_webhook` is not called on shutdown. Both matter with N replicas: every replica runs this lifespan, and dropping updates or removing the webhook during a rolling deploy would discard traffic the other replicas are still serving.

- [ ] **Step 5: Verify all four routes**

```bash
eval "$(scripts/devstack up)" && uv run alembic upgrade head
uv run python -c "
import asyncio, datetime, json
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message
from fastapi.testclient import TestClient
import bot.entrypoints.api as api

sent = []
async def fake(bot, method, timeout=None):
    if isinstance(method, SendMessage):
        sent.append(method.text)
        return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                       chat=Chat(id=method.chat_id, type='private'), text=method.text)
    return True

app = api.create_app()
with TestClient(app) as client:
    app.state.ctx.bot.session.make_request = fake
    app.state.settings.webhook.verify_source_ip = False
    print('live   :', client.get('/health/live').status_code, client.get('/health/live').json())
    r = client.get('/health/ready'); print('ready  :', r.status_code, r.json())
    print('metrics:', client.get('/metrics').status_code)
    upd = {'update_id': 7001, 'message': {'message_id': 1, 'date': 1700000000,
           'chat': {'id': 880004, 'type': 'private'},
           'from': {'id': 880004, 'is_bot': False, 'first_name': 'Ada', 'language_code': 'en'},
           'text': '/start'}}
    print('webhook:', client.post('/webhook', json=upd).status_code, '| replies:', sent)
    print('bad sec:', client.post('/webhook', json=upd,
          headers={'X-Telegram-Bot-Api-Secret-Token': 'wrong'}).status_code, '(403 if secret set)')
"
```

Expected: `live: 200`, `ready: 200 {'status': 'ready', ...}` with both checks `True`, `metrics: 200`, `webhook: 200` with one reply. Stop the Redis container and re-run to confirm `/health/ready` returns 503 while `/health/live` still returns 200.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add -A
git commit -m "feat(api): serve webhook, health, and metrics from a FastAPI entrypoint"
```

---

### Task 13: TaskIQ infrastructure and buffered analytics

**Files:**
- Create: `bot/tasks/__init__.py`, `bot/tasks/analytics.py`, `bot/analytics/buffered.py`, `bot/entrypoints/worker.py`, `bot/entrypoints/scheduler.py`
- Modify: `bot/core/di.py`, `bot/cache/keys.py`

**Interfaces:**
- Consumes: container, `Redis`, `AbstractAnalyticsLogger`.
- Produces: `broker` and `scheduler` in `bot.tasks`; `get_container() -> AsyncContainer` for tasks; `BufferedAnalyticsLogger`; task `flush_analytics`. Task 14 registers more tasks on the same broker.

- [ ] **Step 1: Broker and worker lifecycle**

`bot/tasks/__init__.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from bot.core.config import get_settings
from bot.core.di import create_container
from bot.core.logging import setup_logging

if TYPE_CHECKING:
    from dishka import AsyncContainer

_settings = get_settings()

broker = ListQueueBroker(url=_settings.redis.url).with_result_backend(
    RedisAsyncResultBackend(redis_url=_settings.redis.url),
)

scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])

_container: AsyncContainer | None = None


def get_container() -> AsyncContainer:
    """Container for task bodies. Built once per worker process."""
    if _container is None:
        msg = "container is not initialised - tasks must run inside a taskiq worker"
        raise RuntimeError(msg)
    return _container


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    global _container  # noqa: PLW0603 - one container per worker process
    settings = get_settings()
    setup_logging(settings)
    _container = create_container(settings)
    state.container = _container


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    global _container  # noqa: PLW0603
    if _container is not None:
        await _container.close()
        _container = None


from bot.tasks import analytics  # noqa: E402, F401 - registers tasks on the broker
```

Creating the broker at module scope is required — TaskIQ discovers tasks by importing this module. The *container* is still built lazily on worker startup, so importing `bot.tasks` from the API process to enqueue work does not open a second set of connections.

- [ ] **Step 2: Buffered analytics**

`bot/analytics/buffered.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

import orjson

from bot.analytics.types import AbstractAnalyticsLogger

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from bot.analytics.types import BaseEvent

MAX_BUFFER = 100_000


class BufferedAnalyticsLogger(AbstractAnalyticsLogger):
    """Pushes events onto a Redis list instead of calling the provider inline.

    The old implementation awaited an Amplitude POST before the handler ran and
    raised on a non-200, so a provider outage stopped the bot. This costs one
    Redis round trip and cannot fail the handler.
    """

    def __init__(self, redis: Redis, key: str, max_buffer: int = MAX_BUFFER) -> None:
        self._redis = redis
        self._key = key
        self._max_buffer = max_buffer

    async def log_event(self, event: BaseEvent) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            await pipe.lpush(self._key, orjson.dumps(event.to_dict()))
            await pipe.ltrim(self._key, 0, self._max_buffer - 1)
            await pipe.execute()
```

`LTRIM` bounds the buffer: if the flush task stops, the list drops the oldest events instead of consuming all of Redis's memory.

- [ ] **Step 3: The flush task**

`bot/tasks/analytics.py`:

```python
from __future__ import annotations

import orjson
from loguru import logger
from redis.asyncio import Redis

from bot.analytics.amplitude import AmplitudeTelegramLogger
from bot.core.config import Settings
from bot.tasks import broker, get_container

BATCH = 500


@broker.task(task_name="analytics:flush", schedule=[{"cron": "* * * * *"}])
async def flush_analytics() -> int:
    """Drain the buffer and ship it. Scheduled every minute."""
    container = get_container()
    settings = await container.get(Settings)
    redis = await container.get(Redis)

    if not settings.analytics.amplitude_api_key:
        return 0

    key = settings.analytics.buffer_key
    raw = await redis.rpop(key, BATCH)
    if not raw:
        return 0
    payloads = [raw] if isinstance(raw, bytes) else raw

    sink = AmplitudeTelegramLogger(api_token=settings.analytics.amplitude_api_key)
    sent = 0
    for payload in payloads:
        try:
            await sink.send_raw(orjson.loads(payload))
            sent += 1
        except Exception as exc:  # noqa: BLE001 - one bad event must not stall the batch
            logger.warning(f"analytics event dropped | error: {exc}")
    logger.info(f"analytics flushed | sent: {sent}")
    return sent
```

Add `send_raw` to `bot/analytics/amplitude/client.py` so the flush can post an already-serialized event without rebuilding a `BaseEvent`:

```python
    async def send_raw(self, event: dict[str, Any]) -> None:
        """Send a pre-serialized event dict (used by the buffered flush task)."""
        data = {"api_key": self._api_token, "events": [event]}
        async with (
            ClientSession() as session,
            session.post(self._base_url, headers=self._headers,
                         data=orjson.dumps(data), timeout=self._timeout) as response,
        ):
            self._validate_response(await response.json(content_type="application/json"))
```

with `from typing import Any` added to that file's imports.

- [ ] **Step 4: Bind the buffered logger and add the entrypoints**

In `bot/core/di.py`, replace the `analytics` provider body:

```python
    @provide
    def analytics(self, settings: Settings, redis: Redis) -> AbstractAnalyticsLogger:
        if settings.analytics.amplitude_api_key:
            return BufferedAnalyticsLogger(redis, settings.analytics.buffer_key)
        return NullAnalyticsLogger()
```

with `from bot.analytics.buffered import BufferedAnalyticsLogger`, and drop the now-unused `AmplitudeTelegramLogger` import.

`bot/entrypoints/worker.py`:

```python
"""Worker entrypoint.

Run with: taskiq worker bot.entrypoints.worker:broker
"""

from bot.tasks import broker

__all__ = ["broker"]
```

`bot/entrypoints/scheduler.py`:

```python
"""Scheduler entrypoint. Exactly one instance may run.

Run with: taskiq scheduler bot.entrypoints.scheduler:scheduler
"""

from bot.tasks import scheduler

__all__ = ["scheduler"]
```

- [ ] **Step 5: Verify the buffer fills and the worker drains it**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio
from redis.asyncio import Redis
from bot.analytics.types import AbstractAnalyticsLogger, BaseEvent
from bot.core.config import get_settings
from bot.core.di import create_container

async def main():
    c = create_container(get_settings())
    redis = await c.get(Redis)
    settings = get_settings()
    settings.analytics.amplitude_api_key = 'test-key'   # force the buffered branch
    c2 = create_container(settings)
    gateway = await c2.get(AbstractAnalyticsLogger)
    print('gateway     :', type(gateway).__name__, '(BufferedAnalyticsLogger expected)')
    await redis.delete(settings.analytics.buffer_key)
    for i in range(5):
        await gateway.log_event(BaseEvent(user_id=i, event_type='Sign Up'))
    print('buffered    :', await redis.llen(settings.analytics.buffer_key), '(5 expected)')
    await c2.close(); await c.close()

asyncio.run(main())"
```

Expected: `gateway: BufferedAnalyticsLogger`, `buffered: 5`.

Then confirm a worker boots and picks up the scheduled task:

```bash
eval "$(scripts/devstack up)"
timeout 15 uv run taskiq worker bot.entrypoints.worker:broker --workers 1 2>&1 | tail -20
```

Expected: startup logs listing `analytics:flush` among the registered tasks, and no traceback. `timeout` ending the process is the expected exit.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add -A
git commit -m "feat(tasks): add TaskIQ broker/scheduler and non-blocking buffered analytics"
```

---

### Task 14: Broadcast and background export

**Files:**
- Create: `bot/tasks/broadcast.py`, `bot/tasks/export.py`, `bot/handlers/broadcast.py`
- Modify: `bot/tasks/__init__.py`, `bot/handlers/{__init__,export_users}.py`, `bot/cache/keys.py`

**Interfaces:**
- Consumes: `UserRepository.stream`, `UserService.mark_blocked`, `stream_users_csv`, broker.
- Produces: tasks `broadcast:start(text, initiator_id)`, `broadcast:chunk(broadcast_id, user_ids, text, initiator_id)`, `export:users(chat_id)`; `CacheKeys.broadcast(broadcast_id) -> str`.

- [ ] **Step 1: Add the progress key**

In `bot/cache/keys.py`:

```python
    @classmethod
    def broadcast(cls, broadcast_id: str) -> str:
        return cls._key("broadcast", broadcast_id)
```

- [ ] **Step 2: Create `bot/tasks/broadcast.py`**

```python
from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
from dishka import Scope
from loguru import logger
from redis.asyncio import Redis

from bot.cache.keys import CacheKeys
from bot.database.repositories import UserRepository
from bot.services.users import UserService
from bot.tasks import broker, get_container

CHUNK_SIZE = 500
PROGRESS_TTL = 86_400


@broker.task(task_name="broadcast:start")
async def start_broadcast(broadcast_id: str, text: str, initiator_id: int) -> int:
    """Page the audience and enqueue chunks. Holds at most CHUNK_SIZE ids in memory."""
    container = get_container()
    redis = await container.get(Redis)
    await redis.hset(CacheKeys.broadcast(broadcast_id), mapping={"sent": 0, "blocked": 0, "failed": 0})
    await redis.expire(CacheKeys.broadcast(broadcast_id), PROGRESS_TTL)

    queued = 0
    batch: list[int] = []
    async with container(scope=Scope.REQUEST) as request_container:
        users = await request_container.get(UserRepository)
        async for user in users.stream(batch_size=CHUNK_SIZE):
            if user.is_block:
                continue
            batch.append(user.id)
            if len(batch) >= CHUNK_SIZE:
                await send_chunk.kiq(broadcast_id, batch, text, initiator_id)
                queued += len(batch)
                batch = []
        if batch:
            await send_chunk.kiq(broadcast_id, batch, text, initiator_id)
            queued += len(batch)

    logger.info(f"broadcast queued | id: {broadcast_id} | recipients: {queued}")
    return queued


@broker.task(task_name="broadcast:chunk", retry_on_error=True, max_retries=3)
async def send_chunk(broadcast_id: str, user_ids: list[int], text: str, initiator_id: int) -> None:
    """Send one chunk. Outbound pacing is handled by the session middleware (Task 15)."""
    container = get_container()
    bot = await container.get(Bot)
    redis = await container.get(Redis)
    key = CacheKeys.broadcast(broadcast_id)

    async with container(scope=Scope.REQUEST) as request_container:
        service = await request_container.get(UserService)
        for user_id in user_ids:
            try:
                await bot.send_message(chat_id=user_id, text=text)
            except (TelegramForbiddenError, TelegramNotFound):
                await service.mark_blocked(user_id, value=True)
                await redis.hincrby(key, "blocked", 1)
            except Exception as exc:  # noqa: BLE001 - one bad recipient must not kill the chunk
                logger.warning(f"broadcast send failed | user_id: {user_id} | error: {exc}")
                await redis.hincrby(key, "failed", 1)
            else:
                await redis.hincrby(key, "sent", 1)

    progress = await redis.hgetall(key)
    logger.info(f"broadcast chunk done | id: {broadcast_id} | progress: {progress}")
```

- [ ] **Step 3: Create `bot/tasks/export.py`**

```python
from __future__ import annotations

from aiogram import Bot
from aiogram.types import BufferedInputFile
from dishka import Scope
from loguru import logger

from bot.database.repositories import UserRepository
from bot.tasks import broker, get_container
from bot.utils.users_export import csv_filename, stream_users_csv


@broker.task(task_name="export:users")
async def export_users(chat_id: int) -> int:
    """Stream the users table into a CSV and deliver it."""
    container = get_container()
    bot = await container.get(Bot)

    chunks: list[bytes] = []
    total = 0
    async with container(scope=Scope.REQUEST) as request_container:
        users = await request_container.get(UserRepository)
        async for chunk in stream_users_csv(users.stream()):
            chunks.append(chunk)
        total = await users.count()

    await bot.send_document(
        chat_id=chat_id,
        document=BufferedInputFile(file=b"".join(chunks), filename=csv_filename()),
        caption=f"{total} users",
    )
    logger.info(f"export delivered | chat_id: {chat_id} | users: {total}")
    return total
```

Register both modules in `bot/tasks/__init__.py` by extending the trailing import:

```python
from bot.tasks import analytics, broadcast, export  # noqa: E402, F401 - registers tasks
```

- [ ] **Step 4: Enqueue from handlers**

Rewrite `bot/handlers/export_users.py` so the handler returns immediately:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from aiogram.utils.i18n import gettext as _

from bot.filters.admin import AdminFilter
from bot.tasks.export import export_users

if TYPE_CHECKING:
    from aiogram.types import Message

router = Router(name="export_users")


@router.message(Command(commands="export_users"), AdminFilter())
async def export_users_handler(message: Message) -> None:
    """Queue a CSV export; the worker delivers the file when it is ready."""
    await export_users.kiq(chat_id=message.chat.id)
    await message.answer(_("export queued"))
```

Create `bot/handlers/broadcast.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.utils.i18n import gettext as _

from bot.filters.admin import AdminFilter
from bot.tasks.broadcast import start_broadcast

if TYPE_CHECKING:
    from aiogram.types import Message

router = Router(name="broadcast")


@router.message(Command(commands="broadcast"), AdminFilter())
async def broadcast_handler(message: Message, command: CommandObject) -> None:
    """Queue a broadcast: /broadcast <text>."""
    if not command.args:
        await message.answer(_("usage: /broadcast <text>"))
        return

    broadcast_id = uuid4().hex
    await start_broadcast.kiq(
        broadcast_id=broadcast_id, text=command.args, initiator_id=message.chat.id,
    )
    await message.answer(_("broadcast queued: <code>{id}</code>").format(id=broadcast_id))
```

Register it in `bot/handlers/__init__.py`:

```python
from . import broadcast, export_users, info, menu, start, support
```

```python
    router.include_router(broadcast.router)
```

- [ ] **Step 5: Add the three new translation strings**

`export queued`, `usage: /broadcast <text>`, and `broadcast queued: <code>{id}</code>` need catalogue entries:

```bash
uv run pybabel extract --input-dirs=. -o bot/locales/messages.pot
uv run pybabel update -d bot/locales -i bot/locales/messages.pot
```

Fill in `msgstr` for all three in each of `en`, `ru`, `uk`, then `uv run pybabel compile -d bot/locales`.

- [ ] **Step 6: Verify a broadcast end to end**

Run a worker in one shell and drive it from another.

```bash
# shell 1
eval "$(scripts/devstack up)" && uv run alembic upgrade head
uv run taskiq worker bot.entrypoints.worker:broker --workers 1
```

```bash
# shell 2
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio
from aiogram.types import User as TgUser
from dishka import Scope
from redis.asyncio import Redis
from bot.cache.keys import CacheKeys
from bot.core.config import get_settings
from bot.core.di import create_container
from bot.database.repositories import UserRepository
from bot.tasks import broker
from bot.tasks.broadcast import start_broadcast

async def main():
    c = create_container(get_settings())
    async with c(scope=Scope.REQUEST) as rc:
        repo = await rc.get(UserRepository)
        for i in range(1, 1201):
            if not await repo.exists(i):
                await repo.create(TgUser(id=i, is_bot=False, first_name=f'U{i}'), referrer=None)
        print('audience:', await repo.count())
    await broker.startup()
    bid = 'verify1'
    await start_broadcast.kiq(broadcast_id=bid, text='hello', initiator_id=1)
    await asyncio.sleep(20)
    redis = await c.get(Redis)
    print('progress:', await redis.hgetall(CacheKeys.broadcast(bid)))
    await broker.shutdown(); await c.close()

asyncio.run(main())"
```

Expected: the worker logs `broadcast queued | recipients: 1200` then three `broadcast chunk done` lines. Progress shows a non-zero `failed` count because the example token cannot really send — that is fine and proves error accounting works. What must be true is that the worker never crashes and every recipient is accounted for: `sent + blocked + failed == 1200`.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add -A
git commit -m "feat(tasks): add chunked broadcast with block detection and background export"
```

---

### Task 15: Distributed outbound rate limiting

**Files:**
- Create: `bot/telegram/ratelimit.py`
- Modify: `bot/telegram/factory.py`, `bot/core/di.py`, `bot/cache/keys.py`, `bot/middlewares/metrics.py`

**Interfaces:**
- Consumes: `TokenBucket` (Task 10), `Redis`.
- Produces: `OutboundRateLimiter(global_bucket, chat_bucket)` registered on `Bot.session.middleware`; `CacheKeys.outbound(scope, ident) -> str`; counter `OUTBOUND_WAITS`.

Telegram allows roughly 30 messages per second per bot globally and 1 per second per chat. These are bot-wide limits, so with N API replicas and M workers sending concurrently an in-process limiter cannot see the whole picture. Verified against the installed aiogram: `Bot.session.middleware` is a `RequestMiddlewareManager` exposing `register()`.

- [ ] **Step 1: Add keys and a metric**

In `bot/cache/keys.py`:

```python
    @classmethod
    def outbound(cls, scope: str, ident: int | str) -> str:
        return cls._key("outbound", scope, ident)
```

In `bot/middlewares/metrics.py`:

```python
OUTBOUND_WAITS = Counter(f"{PREFIX}_outbound_rate_limit_waits_total", "Outbound calls delayed.", ["scope"])
OUTBOUND_RETRY_AFTER = Counter(f"{PREFIX}_outbound_retry_after_total", "429s returned by Telegram.")
```

- [ ] **Step 2: Create the session middleware**

`bot/telegram/ratelimit.py`:

```python
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramRetryAfter
from loguru import logger

from bot.cache.keys import CacheKeys
from bot.middlewares.metrics import OUTBOUND_RETRY_AFTER, OUTBOUND_WAITS

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
    from aiogram.methods import TelegramMethod

    from bot.cache.ratelimit import TokenBucket

MAX_RETRY_AFTER = 60


class OutboundRateLimiter:
    """Paces every outgoing Bot API call against shared Redis buckets.

    Registered on the session, so it applies to API replicas and workers alike —
    including sends made from inside a broadcast task.
    """

    def __init__(self, global_bucket: TokenBucket, chat_bucket: TokenBucket) -> None:
        self._global = global_bucket
        self._chat = chat_bucket

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        chat_id = getattr(method, "chat_id", None)

        await self._wait(self._global, CacheKeys.outbound("global", bot.id), "global")
        if chat_id is not None:
            await self._wait(self._chat, CacheKeys.outbound("chat", chat_id), "chat")

        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as exc:
            OUTBOUND_RETRY_AFTER.inc()
            delay = min(exc.retry_after, MAX_RETRY_AFTER)
            logger.warning(f"telegram 429 | sleeping {delay}s | method: {type(method).__name__}")
            await asyncio.sleep(delay)
            return await make_request(bot, method)

    @staticmethod
    async def _wait(bucket: TokenBucket, key: str, scope: str) -> None:
        while (delay := await bucket.acquire(key)) > 0:
            OUTBOUND_WAITS.labels(scope=scope).inc()
            await asyncio.sleep(delay)
```

The 429 path retries exactly once. Retrying in a loop on a limit you are already exceeding makes the problem worse; one retry after the advertised delay covers the common case, and anything beyond that surfaces to the error handler.

- [ ] **Step 3: Attach it to the Bot**

Rewrite `bot/telegram/factory.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.telegram.ratelimit import OutboundRateLimiter

if TYPE_CHECKING:
    from bot.cache.ratelimit import TokenBucket
    from bot.core.config import Settings


def create_bot(settings: Settings, global_bucket: TokenBucket, chat_bucket: TokenBucket) -> Bot:
    bot = Bot(
        token=settings.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware.register(OutboundRateLimiter(global_bucket, chat_bucket))
    return bot
```

In `bot/core/di.py`, add the two buckets and thread them into the `bot` provider. Rename the existing throttle provider's return type so the three buckets stay distinct — dishka resolves by type, so three `TokenBucket` providers would collide. Use `NewType`:

```python
from typing import NewType

ThrottleBucket = NewType("ThrottleBucket", TokenBucket)
GlobalSendBucket = NewType("GlobalSendBucket", TokenBucket)
ChatSendBucket = NewType("ChatSendBucket", TokenBucket)
```

```python
    @provide
    def throttle_bucket(self, redis: Redis, settings: Settings) -> ThrottleBucket:
        rate = 1.0 / settings.bot.rate_limit if settings.bot.rate_limit > 0 else 1.0
        return ThrottleBucket(TokenBucket(redis, rate=rate, capacity=max(1.0, rate), name="throttle"))

    @provide
    def global_send_bucket(self, redis: Redis) -> GlobalSendBucket:
        return GlobalSendBucket(TokenBucket(redis, rate=30.0, capacity=30.0, name="send-global"))

    @provide
    def chat_send_bucket(self, redis: Redis) -> ChatSendBucket:
        return ChatSendBucket(TokenBucket(redis, rate=1.0, capacity=1.0, name="send-chat"))

    @provide
    async def bot(
        self, settings: Settings, global_bucket: GlobalSendBucket, chat_bucket: ChatSendBucket,
    ) -> AsyncIterable[Bot]:
        bot = create_bot(settings, global_bucket, chat_bucket)
        yield bot
        await bot.session.close()
```

Update `bot/middlewares/__init__.py` to resolve `ThrottleBucket` instead of `TokenBucket`, and `bot/middlewares/throttling.py`'s type hint accordingly.

- [ ] **Step 4: Verify pacing holds across two Bot instances**

```bash
eval "$(scripts/devstack up)"
uv run python -c "
import asyncio, time, datetime
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message
from redis.asyncio import Redis
from bot.core.config import get_settings
from bot.core.di import create_container, GlobalSendBucket, ChatSendBucket
from bot.telegram.factory import create_bot

async def fake(bot, method, timeout=None):
    return Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                   chat=Chat(id=getattr(method, 'chat_id', 1), type='private'), text='x')

async def main():
    s = get_settings()
    c = create_container(s)
    redis = await c.get(Redis)
    await redis.flushdb()
    gb, cb = await c.get(GlobalSendBucket), await c.get(ChatSendBucket)
    # Two Bot objects = an api replica and a worker sharing one Redis.
    a, b = create_bot(s, gb, cb), create_bot(s, gb, cb)
    a.session.make_request = fake; b.session.make_request = fake
    start = time.perf_counter()
    for i in range(4):
        await (a if i % 2 == 0 else b).send_message(chat_id=555, text=str(i))
    elapsed = time.perf_counter() - start
    print(f'4 sends to one chat took {elapsed:.2f}s (>=3.0 expected: 1 msg/sec/chat)')
    await a.session.close(); await b.session.close(); await c.close()

asyncio.run(main())"
```

Expected: at least 3 seconds. Without the limiter this completes in milliseconds; the assertion is that two *separate* `Bot` objects still share one bucket.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add -A
git commit -m "feat(telegram): pace outbound API calls with shared Redis token buckets"
```

---

## Verification checklist for the whole plan

Run after Task 15. Every item must pass before the plan is considered done.

- [ ] `uv run ruff check . && uv run ruff format --check .` — clean, and `git diff` empty afterwards
- [ ] `uv run mypy` — clean across the whole `bot` package
- [ ] `uv run python -c "import bot.handlers, bot.core.di"` with **no** `DB_HOST`/`REDIS_HOST` set — succeeds without opening a connection
- [ ] `grep -rn "cachetools\|core.loader\|PickleSerializer\|get_all_users" bot/` — no matches
- [ ] `uv run python -c "import admin.app"` — fails only on the database connection, never on an import
- [ ] `uv run alembic upgrade head` then `uv run alembic check` — reports only the known pre-existing `users.id` unique-constraint drift
- [ ] Two API replicas plus one worker against one Redis: a user's throttle limit is enforced once, not three times
- [ ] The same webhook `update_id` delivered to two replicas produces exactly one reply
- [ ] `/health/live` stays 200 while Redis is stopped; `/health/ready` returns 503
- [ ] `/metrics` is served in both polling and webhook modes

## Out of scope — deliberately left for later plans

Phases 7-12 of the spec: callbacks and keyboards, Telegram Stars payments, the Mini App, the SQLAdmin migration, compose and pgbouncer changes, and documentation. The Flask admin panel, `gunicorn`, `psycopg2-binary`, `tablib`, and the `flask-*` dependencies all remain in place until phase 10.
