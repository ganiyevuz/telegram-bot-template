# Scalable architecture redesign

**Date:** 2026-08-23
**Status:** Approved design, pending implementation plan
**Scope:** Rearchitect `telegram-bot-template` into a multi-replica, webhook-first,
production-grade template.

## 1. Context

The template works, but its architecture assumes a single process that owns all
state. Verification against live Postgres and Redis during the 2026-08-23 upstream
sync surfaced concrete defects:

| # | Defect | Impact |
|---|---|---|
| 1 | `set_language_code()` / `set_is_admin()` never call `clear_cache()` | Writes leave stale reads for the 10s TTL |
| 2 | `get_all_users()` selects the whole table and pickles it into Redis | OOM in two places at scale; `/export_users` sits on it |
| 3 | `ThrottlingMiddleware` uses an in-process `TTLCache` | N replicas grant N x the intended rate limit |
| 4 | `PickleSerializer` is the cache default | Redis write access becomes code execution |
| 5 | `analytics.track_event` awaits the Amplitude POST before the handler and raises on non-200 | A third-party outage stops handlers running |
| 6 | Inline menu buttons (`wallet`, `premium`, `info`, `support`) have no handlers | Dispatcher resolves `['message']` only; buttons are dead |
| 7 | No `dp.errors` handler is registered | Unhandled exceptions vanish; users get silence |
| 8 | `bot/core/loader.py` builds `Bot`, `Dispatcher`, Redis, and i18n at import time | Nothing is testable; importing anything opens connections |
| 9 | `DatabaseMiddleware` opens a session for every update | Connections held for updates that never touch the DB |
| 10 | `/metrics` is mounted only in webhook mode | Polling runs blind |
| 11 | Admin `admin`/`role` tables come from `db.create_all()` at import | Schema outside Alembic; drifts silently |
| 12 | `set_default_commands()` hardcodes English into all three locale dicts | Latent i18n bug |
| 13 | The `premium` button implies monetization that does not exist | Analytics defines `Revenue` events nothing can emit |

Severity levels for defects 3, 5, and 7 are corroborated by the
`telegram-bot-builder` skill's validation checks (no error handler: HIGH;
no rate limiting: MEDIUM; in-memory sessions in production: MEDIUM).

## 2. Goals

- Run N stateless bot replicas behind a load balancer, webhook-first.
- Hold no correctness-critical state in process memory.
- Keep the repository a reusable template: generic `User` model, demo handlers,
  and a fork-and-go story.
- Fix every defect in section 1.
- Monetize via Telegram Stars, wiring up the `premium` button that already exists.
- Ship a Telegram Mini App with a secure backend contract.
- Track latest stable dependency versions.

## 3. Non-goals

- **Tests.** Build for testability; ship no suite. Explicitly deferred.
- Sharded or partitioned update routing (justified only above ~1M active users).
- Multi-bot / multi-tenant support.
- TON Connect / TON blockchain payments. See section 13 for the reasoning.
- A production frontend framework for the Mini App. The template ships a
  dependency-free demo page and documents swapping in React/Vite.
- Clean `git merge upstream/main`. Upstream becomes a cherry-pick source.

## 4. Process topology

One image, four entrypoints.

| Process | Replicas | Command | Responsibility |
|---|---|---|---|
| `api` | N, stateless | `uvicorn bot.entrypoints.api:app` | webhook, `/health/*`, `/metrics`, `/admin` |
| `worker` | M | `taskiq worker bot.tasks:broker` | broadcasts, exports, analytics flush |
| `scheduler` | exactly 1 | `taskiq scheduler bot.tasks:scheduler` | periodic jobs |
| `polling` | dev only | `python -m bot` | long polling; never in production |

Polling cannot be a production path: Telegram permits only one polling process per
bot token, so horizontal scaling requires webhooks.

`gunicorn` is dropped. N containers running uvicorn is simpler to reason about than
gunicorn supervising uvicorn workers.

## 5. Package layout

```
bot/
├── core/            config.py · di.py · logging.py · lifespan.py
├── database/        models/ · repositories/
├── cache/           client.py · keys.py · service.py
├── telegram/        factory.py · ratelimit.py
├── middlewares/     dedup · db · auth · i18n · throttle · logging · metrics · errors
├── handlers/        commands/ · callbacks/ · payments/ · chat_member/
├── webapp/          initdata.py (HMAC validation) · routes.py · static/
├── keyboards/       inline/ · reply/ · pagination.py · callback_data.py
├── filters/ states/
├── services/        use-cases coordinating repos + cache + telegram
├── analytics/       providers behind a Protocol
├── tasks/           broadcast · export · analytics
└── entrypoints/     api.py · worker.py · scheduler.py · polling.py
admin/               SQLAdmin views + auth backend, mounted into the api app
```

## 6. Composition root and dependency injection

`bot/core/loader.py` is **deleted**. Objects are built by dishka providers in
`bot/core/di.py`.

| Scope | Provides |
|---|---|
| `Scope.APP` | `Settings`, Redis pool, `AsyncEngine`, `async_sessionmaker`, `Bot`, `CacheService`, `AnalyticsGateway`, TaskIQ broker |
| `Scope.REQUEST` | `AsyncSession`, repositories, use-case services |

dishka's aiogram integration opens a REQUEST scope per update via middleware and
injects into handlers through `FromDishka[T]`. Providers needing event context
declare `TelegramObject` / `AiogramMiddlewareData` parameters and are registered
alongside `AiogramProvider`.

The `AsyncSession` is resolved lazily: an update whose handler never touches the
database never opens a Postgres connection. This is a direct fix for defect 9.

### Configuration

`Settings` is composed of sub-models, each with an `env_prefix`, so **existing flat
environment variable names are preserved** (`BOT_TOKEN`, `DB_HOST`, `REDIS_PORT`).
Any rename is documented in `.env.example` and the README.

`AMPLITUDE_API_KEY` becomes optional. It is currently required with no default,
so the bot cannot import without it.

## 7. Update pipeline

Outermost first:

| # | Middleware | Rationale |
|---|---|---|
| 1 | `DedupMiddleware` | Redis `SET dedup:{update_id} 1 NX EX 600` before any work |
| 2 | `LoggingMiddleware` | binds `update_id` as correlation id in a loguru contextvar |
| 3 | `MetricsMiddleware` | RED metrics per handler, in every mode |
| 4 | dishka REQUEST scope | opens DI scope; session still not created |
| 5 | `ThrottleMiddleware` | Redis token bucket, per user and per chat |
| 6 | `AuthMiddleware` | user upsert; first consumer of a session |
| 7 | `I18nMiddleware` | locale from cache, then DB |
| 8 | `ChatActionMiddleware` | typing indicator for slow handlers |

Custom context keys are declared by extending aiogram's `MiddlewareData` TypedDict
rather than untyped `data["..."]` access.

**Deduplication must be step 1.** Telegram redelivers a webhook update when a
response is slow, and behind a load balancer the retry reaches a different replica
than the original. Without dedup that is a duplicated `/start`, broadcast opt-in, or
payment. A `SET NX` guard makes handlers idempotent.

**Throttling** is a Redis token bucket implemented as a Lua script so
check-and-consume is atomic. Keyed per user rather than per chat, so members of a
group do not rate-limit each other.

**Errors** are handled by a `dp.errors` handler. `ErrorEvent` carries the exception
and update context; the handler logs with correlation id, tags the Sentry scope, and
replies with a translated apology.

## 8. Data layer

**Repositories** replace free functions: `UserRepository(session)` with explicit
methods. Use-case objects in `services/` own cache invalidation, so
`SetLanguage` invalidating the language key is structural rather than a rule each
call site must remember. This fixes defect 1 by construction.

**`cache/keys.py`** is a typed key registry. Keys carry a version prefix
(`tpl:v1:user:{id}:lang`) so a deploy can invalidate en masse. Invalidation deletes
an explicit key list; `KEYS` is never used, as it blocks Redis.

**Serialization moves from pickle to orjson** over explicit DTOs. Repositories
return domain objects; the cache stores serializable DTOs, never ORM instances.

**`get_all_users()` is deleted.** Reads that span the table use keyset pagination
and `.stream()` with `yield_per`, never materializing more than a page.

## 9. Analytics

`AnalyticsGateway.track()` becomes a non-blocking `LPUSH` to Redis. A scheduled
task drains the buffer and ships batches over a single shared `ClientSession`.
Handler latency becomes one Redis round trip, and a provider outage can no longer
prevent handlers from running. Providers sit behind a `Protocol`; Amplitude,
PostHog, and Google implementations are selected by configuration.

## 10. Outbound rate limiting

Telegram enforces roughly 30 messages per second per bot globally and 1 per second
per chat. These are bot-wide limits, but with N api replicas and M workers sending
concurrently, no in-process limiter can observe the whole picture.

A Redis token bucket (Lua, atomic) is registered on `bot.session.middleware`, which
intercepts every outgoing Bot API call from every process. Two buckets apply:
global (~30/s) and per-chat (1/s). `TelegramRetryAfter` is honored by waiting
`retry_after` rather than retrying immediately.

Verified against the installed aiogram: `AiohttpSession().middleware` is a
`RequestMiddlewareManager` exposing `register()`.

## 11. Background jobs

TaskIQ with a Redis broker and result backend.

**Broadcast** is the feature the template has never had. A parent task keyset-pages
the audience and enqueues chunk tasks; workers send through the shared rate limiter;
`TelegramForbiddenError` flips `is_block = True`; progress is accumulated in Redis
via `HINCRBY` and reported back to the initiator on completion. Retries use
exponential backoff with a dead-letter path.

**Export** streams users into a chunked CSV and sends the document on completion,
replacing the in-memory build.

**Analytics flush** drains the Redis buffer on a schedule.

Blocked users are also detected proactively by a `my_chat_member` handler, which
Telegram sends the moment a user blocks the bot. This keeps audience counts honest
between broadcasts instead of discovering blocks only on send failure.

## 12. Payments — Telegram Stars first

The template has a `premium` button with no handler, while `analytics/types.py`
already defines `"Complete Purchase"`, `"Revenue"`, and a `payment_method` field.
This closes that loop.

**Stars is the default** because it requires no payment provider, no merchant
account, and no fiat onboarding — the lowest-friction path for anyone forking the
template. Classic provider payments work through the same handlers by setting
`PROVIDER_TOKEN` and a fiat currency.

Verified against the installed aiogram: `Bot.send_invoice.provider_token` defaults
to `None`, so a Stars invoice omits it rather than passing an empty-string sentinel.

```python
# bot/handlers/payments/invoice.py
STARS = "XTR"

@router.callback_query(MenuCB.filter(F.action == "premium"))
async def premium(query: CallbackQuery, bot: FromDishka[Bot]) -> None:
    await bot.send_invoice(
        chat_id=query.from_user.id,
        title=_("Premium access"),
        description=_("Unlock all features for 30 days"),
        payload=PremiumPayload(user_id=query.from_user.id, plan="30d").pack(),
        currency=STARS,                                            # no provider_token
        prices=[LabeledPrice(label=_("Premium"), amount=100)],     # 100 Stars
    )
```

`pre_checkout_query` must be answered within Telegram's 10-second window, or the
payment fails:

```python
@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery, payments: FromDishka[PaymentService]) -> None:
    ok, reason = await payments.validate(q.invoice_payload, q.total_amount, q.currency)
    await q.answer(ok=ok, error_message=None if ok else reason)
```

```python
@router.message(F.successful_payment)
async def paid(message: Message, payments: FromDishka[PaymentService]) -> None:
    await payments.record(message.from_user.id, message.successful_payment)
```

**Idempotency** comes from a unique constraint on `telegram_payment_charge_id`.
Update-level dedup only protects against redelivery of the same `update_id`, which
is not the same guarantee.

**Recurring subscriptions** use `create_invoice_link(subscription_period=2592000)`
— verified present in aiogram 3.30. `SuccessfulPayment` then carries
`is_recurring`, `is_first_recurring`, and `subscription_expiration_date`, and
`edit_user_star_subscription` cancels.

**Refunds** use `refund_star_payment(user_id, telegram_payment_charge_id)`, verified
present. Telegram's Stars policy requires merchants to be able to refund, so this is
exposed as an admin action in SQLAdmin rather than left to the API.

**Reconciliation**: a scheduled task calls `get_star_transactions()` and reconciles
against the `Payment` table, catching anything the webhook missed.

**`Payment` model** under Alembic: `id`, `user_id`, `provider`, `currency`,
`amount`, `payload`, `status`, `telegram_payment_charge_id` (unique),
`provider_payment_charge_id`, `is_recurring`, `subscription_expires_at`,
`created_at`.

## 13. Telegram Mini App

**Scope boundary: backend-complete, frontend-minimal.** The security-critical and
scaling-critical parts of a Mini App live on the server, and that is what this
template owns. A production frontend is a separate project with its own toolchain.

### initData validation

The Mini App skill rates *"not validating initData"* as **HIGH severity**: initData
can be forged, so a backend that trusts `initDataUnsafe` lets any user impersonate
any other. Validation is a two-stage HMAC — note that the bot token is the
*message* in the first stage, not the key:

```python
# bot/webapp/initdata.py
def validate_init_data(init_data: str, bot_token: str, max_age: timedelta) -> WebAppUser:
    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received = parsed.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received):
        raise InvalidInitData
    issued = datetime.fromtimestamp(int(parsed["auth_date"]), tz=UTC)
    if datetime.now(UTC) - issued > max_age:
        raise ExpiredInitData
    return WebAppUser.model_validate_json(parsed["user"])
```

Two hardening details beyond the reference implementation: `hmac.compare_digest`
rather than `==`, to avoid a timing oracle on the hash; and an `auth_date`
freshness window, without which a captured initData string is replayable forever.

This is exposed as a FastAPI dependency, so a Mini App route reads
`user: Annotated[WebAppUser, Depends(webapp_user)]` and cannot forget to validate.

**No session tokens.** initData is revalidated on every request rather than
exchanged for a JWT. It is a cheap HMAC, Telegram refreshes it client-side, and it
keeps the api replicas completely stateless — no shared session store, no token
lifecycle, no revocation problem. This is the multi-replica-correct choice.

### API surface

`/api/webapp/me`, `/api/webapp/invoice` (returns a `createInvoiceLink` URL for
`tg.openInvoice`), and referral endpoints — all behind the dependency above. Uses
the same dishka container, repositories, and rate limiter as the bot handlers.

### Launch surfaces

`InlineKeyboardButton(web_app=WebAppInfo(url=...))`, the chat menu button, and
`t.me/<bot>?startapp=<payload>` deep links, which feed the same referral capture
that `/start <referrer>` already uses.

### Demo frontend

One static page served by FastAPI at `/webapp`, no build step and no npm. It
satisfies all five of the skill's Mini App validation checks: the
`telegram-web-app.js` script tag, a viewport meta tag, `tg.ready()`, theme
adaptation through the `--tg-theme-*` CSS variables, and `MainButton` for the
primary action instead of a custom button. It calls `/api/webapp/me` with initData
in an `Authorization` header and buys Premium through `tg.openInvoice`, proving the
whole loop end to end. `HapticFeedback` on the purchase action.

The README documents replacing it with React/Vite; the backend contract does not
change.

### TON Connect is excluded

It drags a wallet SDK and crypto-specific UX into a general-purpose bot template,
and the skill itself flags mobile deep-linking as a HIGH-severity fragile area
requiring real-device testing across iOS, Android, and multiple wallets. Stars
covers monetization for the template's default audience. Documented as an extension
point.

### No new dependencies

initData validation is stdlib `hmac` + `hashlib`. FastAPI serves the static page.

## 14. Keyboards and callbacks

Callback handlers are added for the buttons that currently do nothing. Payloads use
aiogram's `CallbackData` factory rather than bare strings, giving typed and
parseable payloads. A reusable pagination helper is added to
`keyboards/pagination.py`.

Strings resolved outside a request context — notably `set_default_commands()` — use
`lazy_gettext` (`__`). The current implementation hardcodes English into the `en`,
`ru`, and `uk` dictionaries.

## 15. Admin panel

Flask-Admin is replaced by SQLAdmin, which is async and binds to the same
SQLAlchemy models and engine the bot uses, eliminating the parallel sync stack.

- `AuthenticationBackend` provides session login (requires `itsdangerous`).
- `UserAdmin(ModelView)` reproduces the existing list, filter, search, and export.
- `PaymentAdmin(ModelView)` with a refund action.
- `DashboardView(BaseView)` with `@expose("/")` reproduces the stats page.
- `AdminUser` and role tables are created by Alembic migrations, fixing defect 11.
- Passwords are hashed with stdlib `hashlib.scrypt`; no new dependency and no
  hand-rolled scheme.

The admin mounts into the api app, with an environment toggle so a dedicated
admin-only replica remains possible.

## 16. Observability

- `/health/live` — process is up.
- `/health/ready` — Postgres and Redis reachable. A load balancer needs both.
- `/metrics` — mounted in **every** mode, fixing defect 10.

Metrics: updates by type/handler/outcome, handler duration histogram, throttle
drops, dedup hits, outbound API calls and rate-limit waits, broadcast progress,
Mini App initData rejections, payment outcomes, and database pool statistics. The
correlation id is bound in loguru and attached to the Sentry scope. The Grafana
dashboard is extended beyond node-exporter to cover these.

## 17. Deployment

- Compose services: `api` (scalable), `worker` (scalable), `scheduler` (single),
  plus the existing `postgres`, `pgbouncer`, `redis`, `migrator`, `prometheus`,
  `grafana`, `node-exporter`, `pgbackup`. The separate `admin` service is removed.
- Healthchecks are added to every service.
- **pgbouncer switches from session to transaction pooling**, with
  `statement_cache_size=0` and `prepared_statement_cache_size=0` on the SQLAlchemy
  asyncpg engine, and a bounded pool per replica. Session mode with `pool_size=0`
  multiplies Postgres connections by replica count. This change also **removes the
  `CConnection` prepared-statement-name hack entirely**, since that workaround
  exists only to survive session mode.
- Webhook requests are validated by secret token **and** an IP allowlist of
  Telegram's published subnets.
- The Mini App requires HTTPS; the README documents the TLS/reverse-proxy
  requirement, since Telegram will not load a Mini App over plain HTTP and
  localhost will not work on mobile.
- Graceful shutdown drains in-flight updates before exit.

## 18. Dependencies

Added: `fastapi` 0.141.1, `uvicorn` 0.52.4, `sqladmin` 0.31.0, `itsdangerous` 2.2.0,
`python-multipart` 0.0.32, `dishka` 1.10.1, `taskiq` 0.12.5, `taskiq-redis` 1.2.3.

Removed: `flask`, `flask-admin`, `flask-security-too`, `flask-caching`,
`flask-babel`, `flask-sqlalchemy`, `psycopg2-binary`, `gunicorn`, `cachetools`,
`types-cachetools`, `tablib`.

Existing dependencies move to latest stable: `aiogram` 3.30.0, `sqlalchemy` 2.0.52,
`alembic` 1.19.1, `redis` 8.1.0, `pydantic` 2.13.4, `pydantic-settings` 2.15.0,
`orjson` 3.12.0, `prometheus-client` 0.26.0, `sentry-sdk` 2.68.0, `aiohttp` 3.14.3,
`asyncpg` 0.31.0, `uvloop` 0.22.1, `babel` 2.18.0, `loguru` 0.7.3, `ruff` 0.16.4,
`mypy` 2.3.1, `pre-commit` 4.6.2.

Pre-releases are excluded: `sqlalchemy` 2.1.0b3, `pydantic` 2.14.0b1, and
`sentry-sdk` 3.0.0a7 exist but are not stable.

Neither payments nor the Mini App adds a dependency.

Net change: **-11 / +8**, and the entire synchronous database stack disappears.

The Python floor rises from 3.10 to **3.12**; Docker images already run 3.13. The
CI matrix is corrected accordingly — it currently tests 3.9 through 3.12 despite
`requires-python = ">=3.10"`.

The `sqlalchemy[asyncio]` extra from the preceding upstream sync is retained. That
sync also pinned `aiogram` 3.29.1 to escape the yanked 3.29.0; moving to 3.30.0
supersedes that pin while keeping the constraint floor above the yanked release.

## 19. Implementation phases

Each phase leaves the repository working.

1. **Foundation** — config, DI container, lifespan, delete `loader.py`. No behavior change.
2. **Data layer** — repositories, key registry, orjson serialization, streaming reads.
3. **Pipeline** — dedup, throttle, metrics, errors, typed `MiddlewareData`.
4. **Entrypoints** — FastAPI api with health and metrics; polling for development.
5. **Background** — TaskIQ broker, worker, scheduler; analytics buffer; export; broadcast.
6. **Outbound rate limiting** — session middleware token bucket.
7. **Callbacks and keyboards** — `CallbackData`, pagination, `my_chat_member`, `lazy_gettext`.
8. **Payments** — model, migration, Stars handlers, subscriptions, refunds, reconciliation.
9. **Mini App** — initData validation, API routes, launch surfaces, demo page.
10. **Admin** — SQLAdmin, Alembic-managed admin tables, remove the Flask stack.
11. **Ops** — compose, pgbouncer transaction mode, Dockerfile, CI, Grafana.
12. **Docs** — README, `.env.example`, `CLAUDE.md`.

Phase 7 precedes 8 because the payment flow starts from a callback button.

### Plan scoping

**The first implementation plan covers phases 1-6 only** — the scalability core.
Phases 7-12 (payments, Mini App, admin, ops, docs) get their own plans afterwards.

This imposes a constraint on phases 1-6: **the existing Flask-Admin panel must keep
working**, because it is not replaced until phase 10. Concretely:

- SQLAlchemy models stay at `bot/database/models/`, since `admin/app.py` imports
  `bot.database.models.UserModel` directly.
- The `users` table shape does not change in phases 1-6.
- The `admin` compose service and `admin/Dockerfile` remain untouched.
- Only `cachetools` and `types-cachetools` may be removed in phases 1-6 (throttling
  moves to Redis). `flask*`, `psycopg2-binary`, `gunicorn`, and `tablib` are still
  in use by the admin panel and must survive until phase 10.

The `api` entrypoint added in phase 4 runs alongside the existing admin service
rather than absorbing it; the merge happens in phase 10.

## 20. Risks

| Risk | Mitigation |
|---|---|
| SQLAdmin's UI differs from AdminLTE | Feature parity is the bar, not pixel parity; `DashboardView` preserves the stats page |
| pgbouncer transaction mode breaks asyncpg prepared statements | Disable both statement caches; verify against a live pgbouncer before phase 11 lands |
| Payments cannot be end-to-end verified without a real bot and a Stars balance | Handlers are unit-shaped and verified against a mocked bot session; the README documents live verification steps |
| Mini App requires public HTTPS, so local verification is limited | Validation logic is verified directly against known-good and forged initData vectors |
| Python floor 3.10 to 3.12 excludes some forkers | 3.12 is over two years old; Docker already runs 3.13 |
| Upstream merges become cherry-picks | Accepted explicitly; `CLAUDE.md` records every deviation |
| No tests accompany a large rearchitecture | Accepted explicitly; phases are independently runnable, and each is verified against live Postgres and Redis as the sync was |
