# Callbacks, Payments and Mini App Implementation Plan (spec phases 7-9)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire up the inline buttons that currently do nothing, add Telegram Stars payments end to end, and ship a Mini App whose backend cannot be spoofed.

**Architecture:** Callback payloads become typed `CallbackData` factories instead of bare strings. Payments use Stars (`XTR`) by default — no provider token, no merchant account — with idempotency from a unique `telegram_payment_charge_id`. The Mini App is backend-complete and frontend-minimal: `initData` is HMAC-verified on every request, and a single dependency-free demo page proves the loop.

**Tech Stack:** Python 3.14, aiogram 3.30, dishka 1.10, FastAPI 0.141, SQLAlchemy 2.0.52 + Alembic 1.19, TaskIQ 0.12, Redis 8, uv.

**Spec:** `docs/superpowers/specs/2026-08-23-scalable-architecture-design.md` — read sections 12 (Payments), 13 (Mini App) and 14 (Keyboards and callbacks). Also read `docs/superpowers/records/2026-08-23-scalable-core-execution-log.md`, the execution log of phases 1-6: it records 14 defects found in the previous plan's own code and the traps that produced them.

## Global Constraints

- **No tests.** The user has a standing rule: do not write or run tests, and do not create a `tests/` directory. Every task is verified by running the real code against live Postgres and Redis via `scripts/devstack`. If you believe a task needs a test, say so and stop — do not write one.
- **`uv run mypy` must report 0 errors.** There is no known-red baseline; anything you introduce is yours.
- **RUNTIME ANNOTATION HAZARD — this produced 5 of the 14 defects in the previous plan.** dishka calls `get_type_hints()` on `@provide` signatures **and on the entire signature of any injected handler**; aiogram inspects middleware and filter signatures; FastAPI resolves route signatures; TaskIQ inspects task signatures. In any such module every annotated type must be a plain module-level import, never under `if TYPE_CHECKING:` — including `Message`, `CallbackQuery`, `Request` and repositories. Several files carry a deliberate `# ruff: noqa: TC001, TC002`. If ruff suggests moving such an import into a type-checking block, that suggestion is wrong there.
- **`ruff` runs with `fix = true` and `unsafe-fixes = false`.** A bare `ruff check .` rewrites files. After committing, re-run it and confirm `git status --short` is empty — a file ruff rewrites post-commit pollutes every later task's diff.
- **Package manager is `uv`, never bare pip.** Logging is `loguru`, never stdlib `logging`.
- **The Flask admin panel must keep working.** `uv run python -c "import admin.app"` must fail only with a database `OperationalError`, never `ImportError`. Do not touch `admin/`, `Dockerfile`, or `.github/`.
- **Python floor is 3.14.** `ruff format` rewrites `except (A, B):` to PEP 758's `except A, B:`; that is expected, not a defect.
- **Never add a `Co-Authored-By` trailer.** Each commit message must accurately describe its own diff.
- **Treat this plan's code as a specification of intent.** The snippets below were executed before being written down (see "Pre-verified" under each task), but the surrounding integration was not. If something fails, fix it, preserve the described behaviour, and explain the deviation — no silent workarounds, no blanket `# type: ignore`.

## Verification harness (used by every task)

```bash
eval "$(scripts/devstack up)"          # exports DB_*, REDIS_*, BOT_TOKEN
uv run alembic upgrade head
uv run pybabel compile -d bot/locales  # .mo files are gitignored; I18n() raises without them
# ... run your check ...
scripts/devstack down
```

Two techniques that cost the previous plan real time — use them:

- **To exercise the dispatcher without Telegram**, stub `aiogram.client.session.aiohttp.AiohttpSession.make_request` at **class level** *before* importing the lifespan, and answer `GetMe` with a synthetic `User` (the lifespan calls `get_me()` before it yields). Patching `bot.telegram.factory.create_bot` after import does **not** work — `bot/core/di.py` has already bound the symbol.
- **To exercise FastAPI routes**, `starlette.testclient` needs `httpx2`, which is deliberately not a project dependency. Run with `uv run --with httpx2 python <file>.py` — do not add it to the lockfile.
- Write verification scripts to a **file in the repo root**, not `python -c`, or `import bot` may not resolve. Delete them afterwards.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `bot/keyboards/callback_data.py` | Typed `CallbackData` factories — the single source of callback payloads |
| `bot/keyboards/pagination.py` | Reusable paged inline keyboard builder |
| `bot/handlers/callbacks.py` | Handlers for the menu buttons that currently do nothing |
| `bot/handlers/chat_member.py` | `my_chat_member` block/unblock detection |
| `bot/database/models/payment.py` | `PaymentModel` |
| `bot/database/repositories/payment.py` | `PaymentRepository` |
| `bot/services/payments.py` | `PaymentService` — validation, recording, idempotency |
| `bot/handlers/payments.py` | invoice / `pre_checkout_query` / `successful_payment` |
| `bot/tasks/payments.py` | Stars reconciliation against `get_star_transactions` |
| `bot/webapp/__init__.py` `initdata.py` `dependencies.py` `routes.py` | Mini App backend |
| `bot/webapp/static/index.html` | Dependency-free demo page |
| `migrations/versions/*_payments.py` | Alembic migration for `payments` |

**Modified:** `bot/keyboards/inline/menu.py`, `bot/handlers/__init__.py`, `bot/core/config.py`, `bot/core/di.py`, `bot/database/models/__init__.py`, `bot/database/repositories/__init__.py`, `bot/entrypoints/api.py`, `bot/keyboards/default_commands.py`, `bot/tasks/__init__.py`, `.env.example`, `bot/locales/*/LC_MESSAGES/messages.po`.

---

## Phase 7 — Callbacks and keyboards

### Task 1: Typed callback payloads, and wire the dead buttons

**Files:**
- Create: `bot/keyboards/callback_data.py`, `bot/handlers/callbacks.py`
- Modify: `bot/keyboards/inline/menu.py`, `bot/handlers/__init__.py`

**Interfaces:**
- Produces: `MenuCB(CallbackData, prefix="menu")` with fields `action: str` and `page: int = 0`; handlers for actions `wallet`, `premium`, `info`, `support`, `back`.
- Consumes: `main_keyboard()` from `bot/keyboards/inline/menu.py`, `contacts_keyboard`/`support_keyboard` from `bot/keyboards/inline/contacts.py`.

**Why:** `bot/keyboards/inline/menu.py` builds four buttons with bare `callback_data` strings (`wallet`, `premium`, `info`, `support`) and **no handler anywhere**. Confirmed during phase 1-6: `dp.resolve_used_update_types()` returns `['message']` only. Every one of those buttons is dead — a user taps it and nothing happens.

**Pre-verified** (executed against aiogram 3.30 before this plan was written):
```
MenuCB(action="premium", page=2).pack()  -> 'menu:premium:2'
MenuCB.unpack('menu:premium:2')          -> action='premium' page=2
MenuCB.filter()                          -> CallbackQueryFilter
```

- [ ] **Step 1: Create `bot/keyboards/callback_data.py`**

```python
from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    """Payload for the main menu's inline buttons.

    A typed factory rather than bare `callback_data` strings: `pack()` produces
    `menu:<action>:<page>`, and `MenuCB.filter(F.action == "premium")` matches
    without string parsing in the handler.
    """

    action: str
    page: int = 0
```

- [ ] **Step 2: Rebuild the menu keyboard on the factory**

Replace the `buttons` list in `bot/keyboards/inline/menu.py`'s `main_keyboard()` so each button carries `callback_data=MenuCB(action=...).pack()` instead of a raw string. Keep the existing `_()` translated labels and the `keyboard.adjust(1, 1, 2)` layout exactly as they are — only the payloads change.

- [ ] **Step 3: Create `bot/handlers/callbacks.py`**

```python
# ruff: noqa: TC001, TC002  - aiogram and dishka resolve this module's handler
# signatures at runtime via get_type_hints(), so these imports must stay at module level
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.i18n import gettext as _

from bot.keyboards.callback_data import MenuCB
from bot.keyboards.inline.contacts import support_keyboard
from bot.keyboards.inline.menu import main_keyboard

router = Router(name="callbacks")


@router.callback_query(MenuCB.filter(F.action == "info"))
async def info_callback(query: CallbackQuery) -> None:
    await query.message.edit_text(_("about"), reply_markup=main_keyboard())


@router.callback_query(MenuCB.filter(F.action == "support"))
async def support_callback(query: CallbackQuery) -> None:
    await query.message.edit_text(_("support text"), reply_markup=support_keyboard())


@router.callback_query(MenuCB.filter(F.action == "back"))
async def back_callback(query: CallbackQuery) -> None:
    await query.message.edit_text(_("title main keyboard"), reply_markup=main_keyboard())


@router.callback_query(MenuCB.filter(F.action == "wallet"))
async def wallet_callback(query: CallbackQuery) -> None:
    # Placeholder: this template has no wallet domain. Answering explicitly is
    # better than a dead button — the user gets feedback instead of silence.
    await query.answer(_("not available yet"), show_alert=True)
```

`premium` is deliberately absent here — Task 5 adds it as the invoice entry point. Until then it falls through unhandled, which Task 5 closes.

Note `query.message` is `Message | InaccessibleMessage | None` in aiogram 3.30. mypy will flag `edit_text` on that union. Handle it explicitly — check `isinstance(query.message, Message)` and fall back to `query.answer(...)` otherwise. Do **not** silence it with `# type: ignore`.

- [ ] **Step 4: Register the router**

In `bot/handlers/__init__.py`, add `callbacks` to the import list and `router.include_router(callbacks.router)`. Order does not matter here — the callback filters are disjoint from the message handlers.

- [ ] **Step 5: Add the one new translation string**

```bash
uv run pybabel extract --input-dirs=. -o bot/locales/messages.pot
uv run pybabel update -d bot/locales -i bot/locales/messages.pot
```
Set `msgstr` for `not available yet` in all three locales (English: `Not available yet.`), then `uv run pybabel compile -d bot/locales`.

- [ ] **Step 6: Verify the buttons are actually live**

Write this to `cbcheck.py` in the repo root, run it, paste the output, then delete it.

```python
import asyncio, datetime
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetMe, EditMessageText, AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, Update, User

calls = []
async def fake(self, bot, method, timeout=None):
    calls.append(type(method).__name__)
    if isinstance(method, GetMe):
        return User(id=1, is_bot=True, first_name="T", username="t")
    if isinstance(method, EditMessageText):
        return True
    return True
AiohttpSession.make_request = fake

from bot.core.config import get_settings
from bot.core.lifespan import lifespan
from bot.keyboards.callback_data import MenuCB

async def main() -> None:
    async with lifespan(get_settings()) as ctx:
        print("update types:", sorted(ctx.dp.resolve_used_update_types()))
        u = User(id=990001, is_bot=False, first_name="Ada", language_code="en")
        c = Chat(id=990001, type="private")
        msg = Message(message_id=1, date=datetime.datetime.now(datetime.UTC), chat=c, from_user=u, text="menu")
        for action in ("info", "support", "back", "wallet"):
            calls.clear()
            await ctx.dp.feed_update(ctx.bot, Update(
                update_id=hash(action) % 100000,
                callback_query=CallbackQuery(
                    id=action, from_user=u, chat_instance="x",
                    message=msg, data=MenuCB(action=action).pack(),
                ),
            ))
            print(f"  {action:8} -> {calls}")

asyncio.run(main())
```

Expected: `update types` now includes `callback_query` (before this task it was `['message']` only), and each action produces API calls rather than an empty list. `wallet` should show `AnswerCallbackQuery`; the others `EditMessageText`.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/keyboards/callback_data.py bot/handlers/callbacks.py bot/keyboards/inline/menu.py bot/handlers/__init__.py bot/locales
git commit -m "feat(handlers): wire the dead menu buttons to typed CallbackData handlers"
```

---

### Task 2: Reusable pagination for inline keyboards

**Files:**
- Create: `bot/keyboards/pagination.py`

**Interfaces:**
- Consumes: `MenuCB` (Task 1) as the reference payload shape.
- Produces: `paginate(items, page, per_page=5, *, callback_factory, label) -> InlineKeyboardMarkup` and `page_slice(items, page, per_page) -> list`.

**Why:** the template has no way to page a list. Any real bot needs one within a week, and hand-rolling it per feature is how inconsistent navigation happens.

- [ ] **Step 1: Create `bot/keyboards/pagination.py`**

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

T = TypeVar("T")

DEFAULT_PER_PAGE = 5


class PageCallback(Protocol):
    """Builds the callback payload for a given item or navigation target."""

    def __call__(self, *, action: str, page: int) -> str: ...


def page_count(total: int, per_page: int = DEFAULT_PER_PAGE) -> int:
    if total <= 0:
        return 1
    return (total + per_page - 1) // per_page


def page_slice(items: Sequence[T], page: int, per_page: int = DEFAULT_PER_PAGE) -> list[T]:
    """Clamp `page` into range and return that page's items.

    Clamping rather than raising matters: a stale keyboard in an old message can
    send a page number that no longer exists, and a user tapping it should get
    the nearest valid page, not an error.
    """
    last = page_count(len(items), per_page) - 1
    page = max(0, min(page, last))
    start = page * per_page
    return list(items[start : start + per_page])


def paginate(
    items: Sequence[T],
    page: int,
    *,
    callback_factory: PageCallback,
    label: Callable[[T], str],
    per_page: int = DEFAULT_PER_PAGE,
) -> InlineKeyboardMarkup:
    """One button per item on this page, plus a prev/next row when needed."""
    last = page_count(len(items), per_page) - 1
    page = max(0, min(page, last))

    builder = InlineKeyboardBuilder()
    for item in page_slice(items, page, per_page):
        builder.row(
            InlineKeyboardButton(
                text=label(item),
                callback_data=callback_factory(action="item", page=page),
            ),
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=callback_factory(action="page", page=page - 1)))
    if page < last:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=callback_factory(action="page", page=page + 1)))
    if nav:
        builder.row(*nav)

    return builder.as_markup()
```

`Callable` and `Sequence` are imported under `TYPE_CHECKING` here deliberately, and that is safe *in this module only* — nothing introspects `paginate`'s signature at runtime. Contrast that with `bot/core/di.py` and every handler module, where the same move breaks at runtime. If mypy disagrees anywhere here, fix it properly rather than suppressing.

- [ ] **Step 2: Verify the edges**

Write `pagcheck.py` in the repo root, run it, paste output, delete it.

```python
from bot.keyboards.pagination import page_count, page_slice, paginate

items = [f"item-{i}" for i in range(12)]
print("page_count(12, 5) :", page_count(12, 5), "(3 expected)")
print("page_count(0, 5)  :", page_count(0, 5), "(1 expected - never 0)")
print("page 0            :", page_slice(items, 0, 5))
print("page 2            :", page_slice(items, 2, 5), "(2 items)")
print("page 99 clamped   :", page_slice(items, 99, 5), "(same as page 2)")
print("page -1 clamped   :", page_slice(items, -1, 5), "(same as page 0)")

def cb(*, action: str, page: int) -> str:
    return f"demo:{action}:{page}"

m0 = paginate(items, 0, callback_factory=cb, label=str, per_page=5)
m1 = paginate(items, 1, callback_factory=cb, label=str, per_page=5)
m2 = paginate(items, 2, callback_factory=cb, label=str, per_page=5)
print("page 0 nav        :", [b.text for b in m0.inline_keyboard[-1]], "(next only)")
print("page 1 nav        :", [b.text for b in m1.inline_keyboard[-1]], "(prev and next)")
print("page 2 nav        :", [b.text for b in m2.inline_keyboard[-1]], "(prev only)")
```

Expected: counts as annotated; both clamps return a valid page rather than raising or returning empty; the first page offers only `▶️`, the middle both, the last only `◀️`.

- [ ] **Step 3: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/keyboards/pagination.py
git commit -m "feat(keyboards): add a reusable paginated inline keyboard builder"
```

---

### Task 3: Block detection and locale-correct default commands

**Files:**
- Create: `bot/handlers/chat_member.py`
- Modify: `bot/handlers/__init__.py`, `bot/keyboards/default_commands.py`

**Interfaces:**
- Consumes: `UserService.mark_blocked(user_id, *, value)` from `bot/services/users.py`.
- Produces: a `my_chat_member` handler that keeps `is_block` accurate between broadcasts.

**Why two things in one task:** both are small, both live in the "what the bot knows about a user" seam, and both are currently wrong in ways a reviewer would judge together.

**Problem A — blocked users are only discovered by failing.** Today `is_block` is set only when a broadcast send raises. Telegram tells you the moment a user blocks the bot, via `my_chat_member`. Without this handler, audience counts drift and every broadcast re-attempts users who left months ago.

**Problem B — `set_default_commands()` hardcodes English.** `bot/keyboards/default_commands.py` builds `users_commands` with identical English strings under the `en`, `ru` and `uk` keys, then sets them per language. A Russian user sees English command descriptions. It cannot use `gettext` because it runs at startup with no user context — that is exactly what `lazy_gettext` is for.

**Pre-verified:** `from aiogram.utils.i18n import lazy_gettext` imports; `Router` exposes a `my_chat_member` observer; `ChatMemberUpdated` carries `from_user`, `old_chat_member`, `new_chat_member`.

- [ ] **Step 1: Create `bot/handlers/chat_member.py`**

```python
# ruff: noqa: TC001, TC002  - aiogram and dishka resolve this handler's signature at
# runtime via get_type_hints(), so these imports must stay at module level
from aiogram import Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberUpdated
from dishka.integrations.aiogram import FromDishka
from loguru import logger

from bot.services.users import UserService

router = Router(name="chat_member")

_BLOCKED = {ChatMemberStatus.KICKED, ChatMemberStatus.LEFT}


@router.my_chat_member()
async def track_block_state(event: ChatMemberUpdated, users: FromDishka[UserService]) -> None:
    """Keep `is_block` accurate the moment a user blocks or unblocks the bot.

    Telegram sends this as soon as the state changes, so we no longer have to
    discover blocked users by failing to send to them during a broadcast.
    """
    blocked = event.new_chat_member.status in _BLOCKED
    was_blocked = event.old_chat_member.status in _BLOCKED
    if blocked == was_blocked:
        return

    await users.mark_blocked(event.from_user.id, value=blocked)
    logger.info(
        f"chat member state changed | user_id: {event.from_user.id} | blocked: {blocked}",
    )
```

Register it in `bot/handlers/__init__.py` the same way as the other routers.

- [ ] **Step 2: Make the default commands actually translatable**

In `bot/keyboards/default_commands.py`, replace the three duplicated English dictionaries with a single mapping of command name to `lazy_gettext` description, and set it once per locale:

```python
from aiogram.utils.i18n import lazy_gettext as __

users_commands: dict[str, "LazyProxy"] = {
    "help": __("help"),
    "contacts": __("developer contact details"),
    "menu": __("main menu"),
    "settings": __("your settings"),
    "supports": __("support contacts"),
}
```

Then in `set_default_commands`, for each locale in `i18n.available_locales`, resolve the lazy strings under that locale's context before calling `bot.set_my_commands(..., language_code=locale)`. `lazy_gettext` resolves against whatever locale is current, so you must set the context per iteration — aiogram exposes `I18n.context()` / `use_locale()` for this. Confirm which is available in aiogram 3.30 and use it; do not guess.

`set_default_commands` currently takes only `bot`. It will now also need the `I18n` instance — thread it through from `bot/core/lifespan.py`, which already resolves `I18n` from the container.

Add the new msgids to all three `.po` files and translate them for `ru` and `uk`, then compile.

- [ ] **Step 3: Verify both behaviours**

Write `cmcheck.py`, run it, paste output, delete it.

```python
import asyncio, datetime
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus
from aiogram.methods import GetMe, SetMyCommands
from aiogram.types import Chat, ChatMemberMember, ChatMemberBanned, ChatMemberUpdated, Update, User

commands_by_locale = {}
async def fake(self, bot, method, timeout=None):
    if isinstance(method, GetMe):
        return User(id=1, is_bot=True, first_name="T", username="t")
    if isinstance(method, SetMyCommands):
        commands_by_locale[method.language_code] = [c.description for c in method.commands]
    return True
AiohttpSession.make_request = fake

from dishka import Scope
from bot.core.config import get_settings
from bot.core.lifespan import lifespan
from bot.database.repositories import UserRepository

async def main() -> None:
    async with lifespan(get_settings()) as ctx:
        uid = 991234
        u = User(id=uid, is_bot=False, first_name="Ada", language_code="ru")
        c = Chat(id=uid, type="private")
        async with ctx.container(scope=Scope.REQUEST) as rc:
            repo = await rc.get(UserRepository)
            if not await repo.exists(uid):
                await repo.create(u, referrer=None)
        blocked = ChatMemberUpdated(
            chat=c, from_user=u, date=datetime.datetime.now(datetime.UTC),
            old_chat_member=ChatMemberMember(user=u),
            new_chat_member=ChatMemberBanned(user=u, until_date=0),
        )
        await ctx.dp.feed_update(ctx.bot, Update(update_id=991001, my_chat_member=blocked))
        async with ctx.container(scope=Scope.REQUEST) as rc:
            row = await (await rc.get(UserRepository)).get(uid)
            print("is_block after block event :", row.is_block, "(True required)")
        unblocked = ChatMemberUpdated(
            chat=c, from_user=u, date=datetime.datetime.now(datetime.UTC),
            old_chat_member=ChatMemberBanned(user=u, until_date=0),
            new_chat_member=ChatMemberMember(user=u),
        )
        await ctx.dp.feed_update(ctx.bot, Update(update_id=991002, my_chat_member=unblocked))
        async with ctx.container(scope=Scope.REQUEST) as rc:
            row = await (await rc.get(UserRepository)).get(uid)
            print("is_block after unblock     :", row.is_block, "(False required)")
        print("locales that got commands  :", sorted(commands_by_locale))
        en, ru = commands_by_locale.get("en"), commands_by_locale.get("ru")
        print("en == ru descriptions?     :", en == ru, "(False required - ru must be translated)")

asyncio.run(main())
```

Expected: `is_block` flips `True` then back to `False`; all three locales receive commands; and **`en == ru` is `False`** — that last line is the assertion that the hardcoding is gone. Against the current code it prints `True`.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/handlers/chat_member.py bot/handlers/__init__.py bot/keyboards/default_commands.py bot/core/lifespan.py bot/locales
git commit -m "feat(users): detect blocks via my_chat_member and translate default commands"
```

---

## Phase 8 — Telegram Stars payments

### Task 4: `Payment` model, repository, and migration

**Files:**
- Create: `bot/database/models/payment.py`, `bot/database/repositories/payment.py`
- Modify: `bot/database/models/__init__.py`, `bot/database/repositories/__init__.py`, `bot/core/di.py`
- Create: `migrations/versions/<rev>_payments.py` (generated)

**Interfaces:**
- Produces: `PaymentModel`; `PaymentStatus`; `PaymentRepository(session)` with `record(...) -> PaymentModel | None`, `get_by_charge_id(charge_id) -> PaymentModel | None`, `mark_refunded(charge_id) -> None`, `active_subscription(user_id) -> PaymentModel | None`.
- Consumes: `Base`, `big_int_pk`, `created_at` from `bot/database/models/base.py`.

**Idempotency is the whole point.** `telegram_payment_charge_id` carries a **unique constraint**. Update-level dedup (phase 3) only protects against redelivery of the same `update_id`; it does not protect against Telegram sending the same payment through a different update, nor against a retry after a partial failure. The unique constraint is what makes double-crediting impossible.

- [ ] **Step 1: Create `bot/database/models/payment.py`**

```python
from __future__ import annotations

import datetime
import enum

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.models.base import Base, created_at


class PaymentStatus(enum.StrEnum):
    PAID = "paid"
    REFUNDED = "refunded"


class PaymentModel(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("telegram_payment_charge_id", name="uq_payments_charge_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)

    currency: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int]
    payload: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.PAID)

    telegram_payment_charge_id: Mapped[str] = mapped_column(String(128))
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(128), default=None)

    is_recurring: Mapped[bool] = mapped_column(default=False)
    subscription_expires_at: Mapped[datetime.datetime | None] = mapped_column(default=None)

    created_at: Mapped[created_at]
```

Re-export `PaymentModel` and `PaymentStatus` from `bot/database/models/__init__.py`. **This is load-bearing** — Alembic autogenerate reads `Base.metadata` through that import, and a model missing from it produces a migration that *drops* the table.

- [ ] **Step 2: Create `bot/database/repositories/payment.py`**

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from bot.database.models import PaymentModel, PaymentStatus

if TYPE_CHECKING:
    import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        user_id: int,
        currency: str,
        amount: int,
        payload: str,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str | None = None,
        is_recurring: bool = False,
        subscription_expires_at: datetime.datetime | None = None,
    ) -> PaymentModel | None:
        """Insert a payment. Returns None if this charge id was already recorded.

        Mirrors UserRepository.create()'s contract: None means another writer got
        there first, and the session has been rolled back so it stays usable.
        """
        payment = PaymentModel(
            user_id=user_id,
            currency=currency,
            amount=amount,
            payload=payload,
            status=PaymentStatus.PAID,
            telegram_payment_charge_id=telegram_payment_charge_id,
            provider_payment_charge_id=provider_payment_charge_id,
            is_recurring=is_recurring,
            subscription_expires_at=subscription_expires_at,
        )
        self._session.add(payment)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return None
        return payment

    async def get_by_charge_id(self, charge_id: str) -> PaymentModel | None:
        query = select(PaymentModel).filter_by(telegram_payment_charge_id=charge_id)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def mark_refunded(self, charge_id: str) -> None:
        stmt = (
            update(PaymentModel)
            .where(PaymentModel.telegram_payment_charge_id == charge_id)
            .values(status=PaymentStatus.REFUNDED)
        )
        await self._session.execute(stmt)
        await self._session.commit()
```

Re-export from `bot/database/repositories/__init__.py`, and add a REQUEST-scope provider in `bot/core/di.py` alongside `UserRepository`. **Import `PaymentRepository` at module level in `di.py`** — that file carries `# ruff: noqa: TC001, TC002` because dishka resolves `@provide` signatures at runtime.

- [ ] **Step 2b: Note on the `status` column type**

`status` is a plain `String(16)` holding `PaymentStatus` values rather than a database enum. That is deliberate: a native PG enum requires a migration to add a value, which makes adding a payment state later needlessly painful in a template. Keep it a string.

- [ ] **Step 3: Generate and inspect the migration**

```bash
eval "$(scripts/devstack up)"
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "payments"
```

**Read the generated file before committing it.** Autogenerate on this project has a known quirk recorded in the phase 1-6 log: `alembic check` reports phantom drift for a `UniqueConstraint` on `users.id`, because `big_int_pk` sets `unique=True` on a primary key. If that appears in your generated migration, delete those lines — it is pre-existing cosmetic drift, not something this task should "fix" with a schema change.

Then apply and confirm:
```bash
uv run alembic upgrade head
uv run alembic current
```

- [ ] **Step 4: Verify idempotency against a live database**

Write `paycheck.py`, run it, paste output, delete it.

```python
import asyncio
from aiogram.types import User as TgUser
from dishka import Scope
from bot.core.config import get_settings
from bot.core.di import create_container
from bot.database.repositories import PaymentRepository, UserRepository

async def main() -> None:
    c = create_container(get_settings())
    uid = 992001
    async with c(scope=Scope.REQUEST) as rc:
        users = await rc.get(UserRepository)
        if not await users.exists(uid):
            await users.create(TgTgUser := TgUser(id=uid, is_bot=False, first_name="Payer"), referrer=None)
    charge = "charge_abc_123"
    results = []
    for _ in range(3):
        async with c(scope=Scope.REQUEST) as rc:
            repo = await rc.get(PaymentRepository)
            row = await repo.record(
                user_id=uid, currency="XTR", amount=100,
                payload="premium:30d", telegram_payment_charge_id=charge,
            )
            results.append("recorded" if row else "duplicate-ignored")
    print("three records of one charge id:", results)
    async with c(scope=Scope.REQUEST) as rc:
        repo = await rc.get(PaymentRepository)
        found = await repo.get_by_charge_id(charge)
        print("rows for that charge id     :", 1 if found else 0, "(exactly 1 required)")
        await repo.mark_refunded(charge)
        print("status after refund         :", (await repo.get_by_charge_id(charge)).status)
    await c.close()

asyncio.run(main())
```

Expected: `['recorded', 'duplicate-ignored', 'duplicate-ignored']`, exactly one row, and status `refunded`. The second and third calls must **not** raise — a duplicate charge is a normal event, not an error.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
uv run python -c "import admin.app" 2>&1 | tail -1   # OperationalError only
git add bot/database/ bot/core/di.py migrations/versions/
git commit -m "feat(payments): add Payment model, repository and migration"
```

---

### Task 5: Stars invoice, pre-checkout, and successful payment

**Files:**
- Create: `bot/services/payments.py`, `bot/handlers/payments.py`
- Modify: `bot/handlers/__init__.py`, `bot/core/di.py`, `bot/core/config.py`, `.env.example`, `bot/locales/*`

**Interfaces:**
- Consumes: `PaymentRepository` (Task 4), `UserService.set_admin`-style write patterns, `MenuCB` (Task 1), `AbstractAnalyticsLogger`.
- Produces: `PaymentService(payments, users, analytics)` with `validate(payload, amount, currency) -> tuple[bool, str | None]` and `record(user_id, successful_payment) -> bool`; handlers for the invoice, `pre_checkout_query`, and `successful_payment`.

**Pre-verified against aiogram 3.30:**
```
Bot.send_invoice.provider_token default = None   # omit entirely for Stars; do NOT pass ""
PreCheckoutQuery.answer(ok=..., error_message=...)
LabeledPrice(label=..., amount=...)
SuccessfulPayment fields: currency, total_amount, invoice_payload,
    telegram_payment_charge_id, provider_payment_charge_id,
    subscription_expiration_date, is_recurring, is_first_recurring, ...
```

- [ ] **Step 1: Add payment settings**

In `bot/core/config.py`, add a `PaymentSettings(BaseSettings)` sub-model following the existing `env_prefix` pattern, and hang it off `Settings` as `payments`:

```python
class PaymentSettings(BaseSettings):
    model_config = SettingsConfigDict(**_ENV, env_prefix="PAYMENT_")

    currency: str = "XTR"
    premium_price: int = 100          # Stars for a 30-day period
    provider_token: SecretStr | None = Field(default=None, validation_alias="PROVIDER_TOKEN")
    subscription_period_days: int = 30
```

`provider_token` is a `SecretStr` for the same reason `BOT_TOKEN` and `WEBHOOK_SECRET` are — the phase 1-6 log records a real `repr` leak that was fixed by that change. Add `PAYMENT_CURRENCY`, `PAYMENT_PREMIUM_PRICE`, `PROVIDER_TOKEN` and `PAYMENT_SUBSCRIPTION_PERIOD_DAYS` to `.env.example` with a comment noting that Stars (`XTR`) needs **no** provider token.

- [ ] **Step 2: Create `bot/services/payments.py`**

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from bot.analytics.types import BaseEvent, EventProperties

if TYPE_CHECKING:
    from aiogram.types import SuccessfulPayment

    from bot.analytics.types import AbstractAnalyticsLogger
    from bot.core.config import PaymentSettings
    from bot.database.repositories import PaymentRepository
    from bot.services.users import UserService

PREMIUM_PAYLOAD_PREFIX = "premium"


class PaymentService:
    """Owns payment validation and recording, so handlers stay thin.

    `record()` is the only path that credits a user, and it is idempotent by
    construction: the repository's unique charge id decides whether this is a
    first sighting or a replay.
    """

    def __init__(
        self,
        payments: PaymentRepository,
        users: UserService,
        analytics: AbstractAnalyticsLogger,
        settings: PaymentSettings,
    ) -> None:
        self._payments = payments
        self._users = users
        self._analytics = analytics
        self._settings = settings

    def build_payload(self, user_id: int) -> str:
        return f"{PREMIUM_PAYLOAD_PREFIX}:{user_id}:{self._settings.subscription_period_days}d"

    async def validate(self, payload: str, amount: int, currency: str) -> tuple[bool, str | None]:
        """Answer the pre-checkout query. Telegram gives us ~10 seconds.

        Re-check price and currency here rather than trusting the invoice: the
        client controls neither, but a stale invoice from an old price change
        would otherwise be honoured at the old amount.
        """
        if not payload.startswith(f"{PREMIUM_PAYLOAD_PREFIX}:"):
            return False, "Unknown product."
        if currency != self._settings.currency:
            return False, "Unsupported currency."
        if amount != self._settings.premium_price:
            return False, "Price has changed, please reopen the bot and try again."
        return True, None

    async def record(self, user_id: int, payment: SuccessfulPayment) -> bool:
        """Record a payment and credit the user. Returns False if already seen."""
        row = await self._payments.record(
            user_id=user_id,
            currency=payment.currency,
            amount=payment.total_amount,
            payload=payment.invoice_payload,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
            provider_payment_charge_id=payment.provider_payment_charge_id,
            is_recurring=bool(payment.is_recurring),
            subscription_expires_at=payment.subscription_expiration_date,
        )
        if row is None:
            logger.info(f"duplicate payment ignored | charge: {payment.telegram_payment_charge_id}")
            return False

        await self._users.set_premium(user_id, value=True)
        await self._analytics.log_event(
            BaseEvent(
                user_id=user_id,
                event_type="Complete Purchase",
                revenue=payment.total_amount,
                event_properties=EventProperties(payment_method="Crypto"),
            ),
        )
        logger.info(f"payment recorded | user_id: {user_id} | amount: {payment.total_amount}")
        return True
```

`UserService` has no `set_premium` yet — add it alongside `set_admin`, following the same shape (`set_premium(self, user_id: int, *, value: bool)`), writing through `UserRepository` and invalidating the user's cache keys. `UserRepository` needs a matching `set_premium`. **Both writers must invalidate**, for the same reason recorded in the phase 1-6 log: the original `set_language_code`/`set_is_admin` did not, and returned stale reads for the whole TTL window.

Note `EventProperties.payment_method` is a `Literal["Stripe", "PayPal", "Square", "Crypto"]` in `bot/analytics/types.py` — none of those is "Stars". Either add `"Stars"` to that Literal (preferred, it is our own type) or pick the closest existing value and say which you chose and why.

- [ ] **Step 3: Create `bot/handlers/payments.py`**

```python
# ruff: noqa: TC001, TC002  - aiogram, dishka and FastAPI resolve these handler
# signatures at runtime via get_type_hints(); these imports must stay at module level
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.core.config import Settings
from bot.keyboards.callback_data import MenuCB
from bot.services.payments import PaymentService

router = Router(name="payments")


@router.callback_query(MenuCB.filter(F.action == "premium"))
async def send_premium_invoice(
    query: CallbackQuery,
    bot: FromDishka[Bot],
    payments: FromDishka[PaymentService],
    settings: FromDishka[Settings],
) -> None:
    await bot.send_invoice(
        chat_id=query.from_user.id,
        title=_("Premium access"),
        description=_("Unlock premium features for {days} days").format(
            days=settings.payments.subscription_period_days,
        ),
        payload=payments.build_payload(query.from_user.id),
        currency=settings.payments.currency,
        prices=[LabeledPrice(label=_("Premium"), amount=settings.payments.premium_price)],
    )
    await query.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, payments: FromDishka[PaymentService]) -> None:
    ok, reason = await payments.validate(query.invoice_payload, query.total_amount, query.currency)
    await query.answer(ok=ok, error_message=reason)


@router.message(F.successful_payment)
async def payment_succeeded(message: Message, payments: FromDishka[PaymentService]) -> None:
    credited = await payments.record(message.from_user.id, message.successful_payment)
    if credited:
        await message.answer(_("payment received, premium is active"))
```

**Do not pass `provider_token=""` for Stars** — omit the argument. Its default is `None`, and passing an empty string is the JS-example habit that does not apply here.

Note the invoice is sent with `bot.send_invoice(chat_id=...)` rather than `query.message.answer_invoice(...)`, because `query.message` may be an `InaccessibleMessage` and because the invoice should reach the user even if the original message is gone.

- [ ] **Step 4: Wire providers and the router**

Add a REQUEST-scope `PaymentService` provider in `bot/core/di.py` (module-level imports, as always in that file), and register `payments.router` in `bot/handlers/__init__.py`. Register it **before** `callbacks.router` so `MenuCB.filter(F.action == "premium")` here wins — Task 1 deliberately left `premium` unhandled for this.

- [ ] **Step 5: Add the translation strings**

New msgids: `Premium access`, `Unlock premium features for {days} days`, `Premium`, `payment received, premium is active`. Extract, update, translate all three locales, compile.

- [ ] **Step 6: Verify the full flow with a stubbed Telegram**

Write `flowcheck.py`, run it, paste output, delete it. Drive all three stages and assert idempotency.

```python
import asyncio, datetime
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import GetMe, SendInvoice, SendMessage, AnswerPreCheckoutQuery
from aiogram.types import Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update, User
from aiogram.types import CallbackQuery

seen = []
async def fake(self, bot, method, timeout=None):
    seen.append(type(method).__name__)
    if isinstance(method, GetMe):
        return User(id=1, is_bot=True, first_name="T", username="t")
    if isinstance(method, SendMessage):
        return Message(message_id=9, date=datetime.datetime.now(datetime.UTC),
                       chat=Chat(id=method.chat_id, type="private"), text=method.text)
    return True
AiohttpSession.make_request = fake

from dishka import Scope
from bot.core.config import get_settings
from bot.core.lifespan import lifespan
from bot.database.repositories import PaymentRepository, UserRepository
from bot.keyboards.callback_data import MenuCB

async def main() -> None:
    s = get_settings()
    async with lifespan(s) as ctx:
        uid = 993001
        u = User(id=uid, is_bot=False, first_name="Payer", language_code="en")
        c = Chat(id=uid, type="private")
        base = Message(message_id=1, date=datetime.datetime.now(datetime.UTC), chat=c, from_user=u, text="menu")

        seen.clear()
        await ctx.dp.feed_update(ctx.bot, Update(update_id=993101, callback_query=CallbackQuery(
            id="p", from_user=u, chat_instance="x", message=base, data=MenuCB(action="premium").pack())))
        print("1. invoice sent          :", "SendInvoice" in seen, seen)

        seen.clear()
        await ctx.dp.feed_update(ctx.bot, Update(update_id=993102, pre_checkout_query=PreCheckoutQuery(
            id="q", from_user=u, currency=s.payments.currency,
            total_amount=s.payments.premium_price,
            invoice_payload=f"premium:{uid}:{s.payments.subscription_period_days}d")))
        print("2. pre-checkout answered :", "AnswerPreCheckoutQuery" in seen)

        seen.clear()
        await ctx.dp.feed_update(ctx.bot, Update(update_id=993103, pre_checkout_query=PreCheckoutQuery(
            id="q2", from_user=u, currency=s.payments.currency,
            total_amount=s.payments.premium_price + 1,   # wrong price
            invoice_payload=f"premium:{uid}:30d")))
        print("3. wrong price rejected  : answered =", "AnswerPreCheckoutQuery" in seen)

        sp = SuccessfulPayment(currency=s.payments.currency, total_amount=s.payments.premium_price,
                               invoice_payload=f"premium:{uid}:30d",
                               telegram_payment_charge_id="charge_flow_1")
        paid = Message(message_id=2, date=datetime.datetime.now(datetime.UTC), chat=c,
                       from_user=u, successful_payment=sp)
        await ctx.dp.feed_update(ctx.bot, Update(update_id=993104, message=paid))
        await ctx.dp.feed_update(ctx.bot, Update(update_id=993105, message=paid.model_copy()))

        async with ctx.container(scope=Scope.REQUEST) as rc:
            row = await (await rc.get(UserRepository)).get(uid)
            pay = await (await rc.get(PaymentRepository)).get_by_charge_id("charge_flow_1")
            print("4. is_premium            :", row.is_premium, "(True required)")
            print("5. rows for that charge  :", 1 if pay else 0, "(exactly 1 - replay ignored)")

asyncio.run(main())
```

Expected: an invoice is sent; both pre-checkout queries are answered (the wrong-price one with `ok=False`); `is_premium` becomes `True`; and **the second delivery of the same payment creates no second row** — that is the idempotency assertion.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/services/payments.py bot/handlers/payments.py bot/handlers/__init__.py bot/core/ bot/database/ .env.example bot/locales
git commit -m "feat(payments): Telegram Stars invoice, pre-checkout and crediting"
```

---

### Task 6: Recurring Stars subscriptions and refunds

**Files:**
- Modify: `bot/services/payments.py`, `bot/handlers/payments.py`
- Create: `bot/handlers/admin_payments.py`

**Interfaces:**
- Consumes: `PaymentService` (Task 5), `AdminFilter` from `bot/filters/admin.py`.
- Produces: `PaymentService.subscription_link(user_id) -> str`, `PaymentService.refund(user_id, charge_id) -> bool`; an admin `/refund <charge_id>` command.

**Pre-verified against aiogram 3.30:**
```
Bot.create_invoice_link(..., subscription_period=...)   # present; send_invoice does NOT take it
Bot.refund_star_payment(user_id, telegram_payment_charge_id)
SuccessfulPayment.is_recurring / .is_first_recurring / .subscription_expiration_date
```

**Why refunds are not optional.** Telegram's Stars policy requires a merchant to be able to refund. Exposing it as an admin command rather than leaving it to raw API calls is the difference between a template someone can operate and one they cannot.

- [ ] **Step 1: Add subscription link support to `PaymentService`**

```python
    async def subscription_link(self, bot: Bot, user_id: int) -> str:
        """A recurring Stars subscription link.

        Note `subscription_period` exists on `create_invoice_link` but NOT on
        `send_invoice` — recurring Stars must go through a link, which the client
        opens with `tg.openInvoice(...)` or which you send as a URL button.
        """
        period = self._settings.subscription_period_days * 86400
        if period != STARS_SUBSCRIPTION_PERIOD_SECONDS:
            # Bot API: `subscription_period` "must always be 2592000 (30 days)".
            # Failing here names the setting; letting it through produces an opaque
            # "Bad Request: invalid subscription period" from Telegram instead.
            msg = (
                f"PAYMENT_SUBSCRIPTION_PERIOD_DAYS must be 30 for Stars subscriptions, "
                f"got {self._settings.subscription_period_days}"
            )
            raise ValueError(msg)

        return await bot.create_invoice_link(
            title="Premium subscription",
            description=f"Renews every {self._settings.subscription_period_days} days",
            payload=self.build_payload(user_id),
            currency=self._settings.currency,
            prices=[LabeledPrice(label="Premium", amount=self._settings.premium_price)],
            subscription_period=period,
        )
```

Import `Bot` and `LabeledPrice` at module level in that file if they are not already there, and add the module constant `STARS_SUBSCRIPTION_PERIOD_SECONDS = 2592000` beside `PREMIUM_PAYLOAD_PREFIX`.

**Do not import `Bot` under `if TYPE_CHECKING:`.** `bot/services/payments.py` currently keeps its type-only imports there and that is fine — dishka builds `PaymentService` through an explicit factory in `bot/core/di.py`, so it never introspects `__init__`. `Bot` here is only a parameter annotation on a plain method, so either placement works at runtime; put it under `TYPE_CHECKING` with the others for consistency, and let ruff decide.

- [ ] **Step 2: Add refund support to `PaymentService`**

```python
    async def refund(self, bot: Bot, user_id: int, charge_id: str) -> bool:
        """Refund a Stars payment and revoke premium.

        Returns False when the charge is unknown to us — refunding a charge we
        never recorded would revoke premium the user paid for elsewhere.
        """
        existing = await self._payments.get_by_charge_id(charge_id)
        if existing is None or existing.user_id != user_id:
            return False

        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
        await self._payments.mark_refunded(charge_id)
        await self._users.set_premium(user_id, value=False)
        logger.info(f"payment refunded | user_id: {user_id} | charge: {charge_id}")
        return True
```

- [ ] **Step 3: Record subscription renewals distinctly**

In `payment_succeeded` (Task 5), a renewal arrives as another `successful_payment` with `is_recurring=True` and a fresh `telegram_payment_charge_id`. The existing `record()` already handles it — each renewal is a new row, and `subscription_expires_at` comes from `subscription_expiration_date`. Confirm the reply distinguishes a first purchase from a renewal using `is_first_recurring`, so a user is not told "premium is active" every month as though it were new.

- [ ] **Step 4: Create `bot/handlers/admin_payments.py`**

```python
# ruff: noqa: TC001, TC002  - runtime-resolved handler signature
from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _
from dishka.integrations.aiogram import FromDishka

from bot.filters.admin import AdminFilter
from bot.services.payments import PaymentService

router = Router(name="admin_payments")


@router.message(Command(commands="refund"), AdminFilter())
async def refund_command(
    message: Message,
    command: CommandObject,
    bot: FromDishka[Bot],
    payments: FromDishka[PaymentService],
) -> None:
    """/refund <user_id> <charge_id>"""
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        # No angle brackets: the bot's default parse mode is HTML (see
        # bot/telegram/factory.py), so "<user_id>" would be read as an unknown HTML
        # tag and Telegram would reject the whole message with "can't parse entities".
        await message.answer(_("usage: /refund USER_ID CHARGE_ID"))
        return

    ok = await payments.refund(bot, int(parts[0]), parts[1])
    await message.answer(_("refunded") if ok else _("unknown charge id"))
```

Register the router. Add the three new msgids to all locales and compile.

- [ ] **Step 5: Verify refund and renewal handling**

Write `refundcheck.py`, run it, paste output, delete it. It must cover: a refund of a recorded charge succeeds and clears `is_premium`; a refund of an unknown charge returns False and does **not** clear `is_premium`; a refund of a charge belonging to a **different** user returns False (the `existing.user_id != user_id` branch); a renewal creates a second row without a duplicate error; and `/refund` with bad arguments produces a message Telegram actually accepts under HTML parse mode. Stub `RefundStarPayment` in the session fake to return `True`.

Pair every assertion with a guard proving the code under test ran — e.g. assert `is_premium` was `True` *before* the unknown-charge refund, otherwise "still not premium" passes vacuously.

The unknown-charge case is the one that matters — an admin fat-fingering a charge id must not silently revoke a paying user's access.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/services/payments.py bot/handlers/ bot/locales
git commit -m "feat(payments): recurring Stars subscriptions and an admin refund command"
```

---

### Task 7: Stars reconciliation

**Files:**
- Create: `bot/tasks/payments.py`
- Modify: `bot/tasks/__init__.py`

**Interfaces:**
- Consumes: `get_container()` from `bot/tasks/__init__.py`, `PaymentRepository` (Task 4).
- Produces: scheduled task `payments:reconcile`.

**Why:** webhooks are lossy. The phase 1-6 work proved a webhook can return 200 while an update is dropped (a Redis outage does exactly that). A payment lost that way means a user paid and got nothing. `get_star_transactions` is the authoritative ledger; reconciling against it turns a silent loss into a self-healing gap.

**Pre-verified:** `Bot.get_star_transactions(offset, limit)`; `StarTransaction` fields are `id`, `amount`, `date`, `nanostar_amount`, `source`, `receiver`.

- [ ] **Step 1: Create `bot/tasks/payments.py`**

```python
from __future__ import annotations

from aiogram import Bot
from dishka import Scope
from loguru import logger

from bot.database.repositories import PaymentRepository
from bot.tasks import broker, get_container

BATCH = 100


@broker.task(task_name="payments:reconcile", schedule=[{"cron": "*/15 * * * *"}])
async def reconcile_star_payments() -> int:
    """Find Stars charges Telegram recorded that we never did.

    A webhook can return 200 while the update is dropped — a Redis outage does
    exactly that — so a payment can exist on Telegram's side and not on ours.
    This finds those gaps rather than waiting for a user to complain.
    """
    container = get_container()
    bot = await container.get(Bot)

    missing = 0
    transactions = await bot.get_star_transactions(offset=0, limit=BATCH)
    async with container(scope=Scope.REQUEST) as request_container:
        payments = await request_container.get(PaymentRepository)
        for tx in transactions.transactions:
            # `source` is set for incoming payments; outgoing refunds have `receiver`.
            if tx.source is None:
                continue
            if await payments.get_by_charge_id(tx.id) is None:
                missing += 1
                logger.warning(
                    f"star transaction not recorded locally | id: {tx.id} | amount: {tx.amount}",
                )

    if missing:
        logger.error(f"reconciliation found {missing} unrecorded Stars payments")
    return missing
```

**Report, do not auto-credit.** Crediting from reconciliation would need the user id and payload, and `StarTransaction.source` does not reliably carry the invoice payload — guessing would risk granting premium for the wrong product. Logging a warning per gap, loudly, is the honest behaviour for a template. Say in your report if you find the payload is in fact reliably available, in which case auto-crediting becomes a defensible follow-up.

Register the module in `bot/tasks/__init__.py`'s trailing import so TaskIQ discovers it.

- [ ] **Step 1b: Expire premium that has run out**

This step exists because the Task 5 review found that `is_premium` is a boolean nothing ever
clears: the invoice promises "premium for 30 days" and then grants it forever. The Task 5 fix
round makes `subscription_expires_at` actually get written; **this is the half that enforces
it**, and without it that column is decoration.

Add to `bot/database/repositories/payment.py`:

```python
    async def expire_premium(self) -> int:
        """Clear `is_premium` for users with no unexpired paid period left.

        One statement rather than a read-then-write loop: this runs on a schedule
        across several replicas, and a loop would race with itself.
        """
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        still_paid = (
            select(PaymentModel.id)
            .where(
                PaymentModel.user_id == UserModel.id,
                PaymentModel.status == PaymentStatus.PAID,
                PaymentModel.subscription_expires_at.is_not(None),
                PaymentModel.subscription_expires_at > now,
            )
            .exists()
        )
        stmt = (
            update(UserModel)
            .where(UserModel.is_premium.is_(True), ~still_paid)
            .values(is_premium=False)
            .returning(UserModel.id)
        )
        expired = list((await self._session.execute(stmt)).scalars())
        await self._session.commit()
        return len(expired)
```

Note `subscription_expires_at.is_not(None)` is load-bearing: a row with a NULL expiry must not
count as "still paid", or a single legacy row would keep a user premium forever.

`expire_premium` returns the count, but the **cache must be invalidated for each expired user**
or `UserService.is_premium()` keeps serving `True` from Redis until its TTL lapses. Have the
repository method return the list of ids and have the task invalidate them — resolve
`CacheService` from the same REQUEST container and call `invalidate_user(user_id)` per id.
Adjust the snippet's return type accordingly and say in your report which shape you chose.

Then add a second scheduled task in `bot/tasks/payments.py`:

```python
@broker.task(task_name="payments:expire_premium", schedule=[{"cron": "7 * * * *"}])
async def expire_premium() -> int:
```

Hourly at minute 7, not on the hour — spreading scheduled work off the top of the hour avoids
piling every cron job onto the same tick.

**Verify it with a contrast, not a bare assertion.** Create three users: one whose expiry is in
the past, one whose expiry is in the future, and one whose payment is REFUNDED. Assert
`is_premium` before the sweep is `True` for all three (the guard), then that the sweep clears
exactly the first and third and leaves the second alone. A sweep that clears everyone passes a
one-user test perfectly.

- [ ] **Step 2: Verify it runs and detects a gap**

Write `reconcheck.py`, run it, paste output, delete it. Stub `GetStarTransactions` to return two transactions — one whose `id` you have already recorded via `PaymentRepository.record()`, one you have not — and assert the task returns `1` and logs the unrecorded one.

- [ ] **Step 3: Verify the worker still boots**

```bash
eval "$(scripts/devstack up)"
timeout 15 uv run taskiq worker bot.entrypoints.worker:broker --workers 1 2>&1 | tail -20
```
Expected: startup logs listing `payments:reconcile` among the registered tasks, no traceback. `timeout` ending the process is the expected exit. Note `timeout` may not exist on macOS — use `gtimeout` or run it in the background and kill it.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/tasks/
git commit -m "feat(payments): reconcile local records against Telegram's Stars ledger"
```

---

## Phase 9 — Telegram Mini App

### Task 8: `initData` validation

**Files:**
- Create: `bot/webapp/__init__.py`, `bot/webapp/initdata.py`
- Modify: `bot/core/config.py`, `.env.example`

**Interfaces:**
- Produces: `WebAppUser` (pydantic model: `id: int`, `first_name: str`, `last_name: str | None`, `username: str | None`, `language_code: str | None`, `is_premium: bool = False`); `validate_init_data(init_data: str, bot_token: str, max_age: timedelta) -> WebAppUser`; `InvalidInitData` and `ExpiredInitData` exceptions.
- Consumes: nothing — this module is deliberately dependency-free so it can be reasoned about in isolation.

**This is the security-critical module of phase 9.** `initData` is client-supplied. A backend that trusts `initDataUnsafe` lets any user impersonate any other. Everything else in this phase depends on this function being right.

**PRE-VERIFIED — the algorithm below was executed against real vectors before this plan was written.** Results:
```
valid initData   -> {'id': 42, 'first_name': 'Ada'}
wrong bot token  -> rejected (bad hash)
tampered user    -> rejected (bad hash)
stale auth_date  -> rejected (expired)
```
Note the two-stage HMAC: the **bot token is the message** in the first stage, not the key. Getting that backwards produces a function that rejects everything, which is easy to mistake for "working securely".

- [ ] **Step 1: Add the setting**

In `bot/core/config.py`, extend `WebhookSettings` or add a small `WebAppSettings` with:
```python
    init_data_max_age_seconds: int = Field(default=86400, validation_alias="WEBAPP_INIT_DATA_MAX_AGE")
```
Add it to `.env.example` with a comment: this bounds replay of a captured `initData` string.

- [ ] **Step 2: Create `bot/webapp/initdata.py`**

```python
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from pydantic import BaseModel


class InvalidInitData(Exception):
    """The signature did not verify — the data is forged or the token is wrong."""


class ExpiredInitData(Exception):
    """The signature verified but the data is older than we accept."""


class WebAppUser(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool = False


def validate_init_data(init_data: str, bot_token: str, max_age: timedelta) -> WebAppUser:
    """Verify Telegram Mini App initData and return the user it names.

    Two-stage HMAC, per Telegram's spec: the bot token is the MESSAGE in the
    first stage (keyed by the literal b"WebAppData"), and the resulting digest
    is the KEY for the second stage over the sorted data-check string.
    """
    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received = parsed.pop("hash", "")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    # Constant-time: `==` short-circuits on the first differing byte and leaks the
    # signature to a timing attack. This project already learned that the hard way
    # on the webhook secret — see the phase 1-6 execution log.
    if not hmac.compare_digest(expected, received):
        raise InvalidInitData

    # Without this, a captured initData string is replayable forever.
    issued = datetime.fromtimestamp(int(parsed["auth_date"]), tz=UTC)
    if datetime.now(UTC) - issued > max_age:
        raise ExpiredInitData

    return WebAppUser.model_validate_json(parsed["user"])
```

`parse_qsl(..., strict_parsing=True)` is deliberate: a malformed query string raises rather than silently yielding a partial dict that might still hash.

- [ ] **Step 3: Verify against forged and stale vectors**

Write `initcheck.py`, run it, paste output, delete it. Construct the vectors yourself rather than hardcoding a captured string, so the test does not rot:

```python
import hashlib, hmac, json, time
from datetime import timedelta
from urllib.parse import urlencode

from bot.webapp.initdata import ExpiredInitData, InvalidInitData, validate_init_data

TOKEN = "110201544:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
MAX_AGE = timedelta(days=1)

def sign(payload: dict, token: str) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

user = json.dumps({"id": 42, "first_name": "Ada"}, separators=(",", ":"))
payload = {"auth_date": str(int(time.time())), "query_id": "AAF", "user": user}
good = urlencode({**payload, "hash": sign(payload, TOKEN)})

print("valid            ->", validate_init_data(good, TOKEN, MAX_AGE))
for label, data, token in [
    ("wrong token", good, "999:WRONGTOKEN"),
    ("tampered user", good.replace("Ada", "Eve"), TOKEN),
    ("no hash", urlencode(payload), TOKEN),
]:
    try:
        validate_init_data(data, token, MAX_AGE)
        print(f"{label:16} -> ACCEPTED (SECURITY FAILURE)")
    except InvalidInitData:
        print(f"{label:16} -> rejected")

old = {**payload, "auth_date": str(int(time.time()) - 90000)}
try:
    validate_init_data(urlencode({**old, "hash": sign(old, TOKEN)}), TOKEN, MAX_AGE)
    print("stale auth_date  -> ACCEPTED (SECURITY FAILURE)")
except ExpiredInitData:
    print("stale auth_date  -> rejected")
```

Every line except the first must print `rejected`. Any `ACCEPTED` is a hard stop — do not proceed to Task 9.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/webapp/ bot/core/config.py .env.example
git commit -m "feat(webapp): verify Telegram Mini App initData with constant-time HMAC"
```

---

### Task 9: Mini App API surface

**Files:**
- Create: `bot/webapp/dependencies.py`, `bot/webapp/routes.py`
- Modify: `bot/entrypoints/api.py`, `bot/middlewares/metrics.py`

**Interfaces:**
- Consumes: `validate_init_data`, `WebAppUser`, `InvalidInitData`, `ExpiredInitData` (Task 8); `UserService`, `PaymentService`.
- **Ordering:** this task needs `PaymentService.subscription_link`, which Task 6 adds. Task 6 must land first.
- Produces: FastAPI dependency `webapp_user(...) -> WebAppUser`; routes `GET /api/webapp/me` and `POST /api/webapp/invoice`; counter `WEBAPP_INIT_DATA_REJECTIONS`.

**No session tokens, deliberately.** `initData` is revalidated on every request rather than exchanged for a JWT: it is a cheap HMAC, Telegram refreshes it client-side, and it keeps the API replicas completely stateless — no shared session store, no token lifecycle, no revocation problem. That is the multi-replica-correct choice and it matches everything else in this architecture.

- [ ] **Step 0: Wire dishka into FastAPI — it is not wired today**

`bot/core/lifespan.py:53` calls `setup_dishka(container=container, router=dp, auto_inject=True)`.
That is the **aiogram** integration. Nothing wires dishka into FastAPI: the existing routes
(`health`, `metrics`, `webhook`) never resolve a service, so the gap has never mattered. Your
routes are the first that need `UserService` and `PaymentService`.

**The obvious wiring does not work.** `dishka.integrations.fastapi.setup_dishka` is two
statements — `app.add_middleware(ContainerMiddleware)` and
`app.state.dishka_container = container` — and the container only exists once the lifespan has
started. Calling `setup_dishka` inside `_lifespan` fails, verified:

```
add_middleware INSIDE lifespan: RAISED RuntimeError: Cannot add middleware after an application has started
```

Starlette builds its middleware stack before the lifespan runs. Split the two halves:

- `app.add_middleware(ContainerMiddleware)` in `create_app()`, at construction time.
- `app.state.dishka_container = ctx.container` inside `_lifespan`, beside the existing
  `app.state.ctx = ctx`.

Then declare the webapp router as `APIRouter(route_class=DishkaRoute)` and use `FromDishka[...]`
in route signatures, exactly as the aiogram handlers do. Verified working end to end with both
APP- and REQUEST-scoped dependencies:

```
lifespan startup: lifespan.startup.complete
WITH lifespan: 200 {"g":"hello","per":["hello","req"]}
```

Import `ContainerMiddleware`, `DishkaRoute` and `FromDishka` from `dishka.integrations.fastapi`.

- [ ] **Step 1: Create `bot/webapp/dependencies.py`**

The dependency reads `initData` from an `Authorization: tma <initData>` header — the scheme Telegram documents for this — and returns a verified `WebAppUser`. Because it is a FastAPI dependency, **every annotated type in this module must be a module-level import**: FastAPI resolves signatures at runtime. Add the per-file `# ruff: noqa: TC001, TC002`.

It must raise `HTTPException(401)` for both `InvalidInitData` and `ExpiredInitData`, without distinguishing them in the response body — telling a caller *why* their forgery failed is free information. Log the distinction server-side, and increment `WEBAPP_INIT_DATA_REJECTIONS.labels(reason=...)` so a spike in forgery attempts is visible.

Add that counter to `bot/middlewares/metrics.py` next to the existing ones, labelled `reason` with a **bounded** set of values (`invalid`, `expired`, `missing`) — never the raw header, which would be unbounded cardinality.

- [ ] **Step 2: Create `bot/webapp/routes.py`**

`GET /api/webapp/me` returns the caller's own profile — resolve `UserService` from dishka and return id, first name, language and premium status. It must return only the *caller's* data; there is no user-id parameter, because a route that accepts one is a route someone will forget to authorise.

`POST /api/webapp/invoice` returns a `createInvoiceLink` URL for the client to open with `tg.openInvoice(...)`. Reuse `PaymentService.subscription_link` from Task 6 rather than duplicating invoice construction.

**`subscription_link`'s title and description are hardcoded English on purpose, and fixing that is this task's job.** aiogram's `gettext` resolves through a ContextVar that only an aiogram middleware sets, so calling `_()` from a FastAPI route raises rather than translating. Wrap the call the same way Task 3 did for default commands — `with i18n.context(), i18n.use_locale(locale):` — resolving the caller's locale from `UserService.language_of(user.id)` and falling back to `DEFAULT_LOCALE`. Then change those two strings in `bot/services/payments.py` to use `_()`. Resolve the `I18n` instance from dishka; it is already an APP-scoped provider.

Both routes depend on `webapp_user`. Mount the router in `bot/entrypoints/api.py` alongside health, metrics and webhook.

- [ ] **Step 3: Serve the demo page**

Mount `bot/webapp/static/` at `/webapp` with FastAPI's `StaticFiles`, `html=True` so `/webapp` serves `index.html`. Task 10 writes that file; create the directory with a placeholder `index.html` containing a single `<h1>` so this task's mount is verifiable on its own.

- [ ] **Step 4: Verify authentication end to end**

Write `webappcheck.py`, run it with `uv run --with httpx python webappcheck.py`, paste output, delete it. (`httpx` is NOT a project dependency — do not `uv add` it for a throwaway check; `--with` installs it for that one invocation.)

**`httpx.ASGITransport` does NOT run the lifespan.** It never sends the ASGI lifespan messages,
so `_lifespan` never executes, `app.state.ctx` and `app.state.dishka_container` are never set,
and every route fails with `AttributeError: 'State' object has no attribute 'dishka_container'`.
Verified. This is the dangerous kind of harness bug — a confident 500 that reads as "my route is
broken" when the route is fine, and it will push you into "fixing" working code.

Drive the lifespan explicitly. Either `with TestClient(app) as client:` (the context manager form
runs startup and shutdown), or drive it yourself before issuing requests:

```python
recv, send = asyncio.Queue(), asyncio.Queue()
await recv.put({"type": "lifespan.startup"})
task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, recv.get, send.put))
assert (await send.get())["type"] == "lifespan.startup.complete"
# ... issue requests through httpx.ASGITransport(app=app) ...
await recv.put({"type": "lifespan.shutdown"})
await task
```

The lifespan builds the whole container, so you need a real Postgres and Redis — use PRIVATE
containers (`t9-pg` / `t9-rd` on unused ports), never the shared `tbt-devstack-*` pair. Set
`USE_WEBHOOK=False` so startup does not try to reach Telegram. It must show:
- a request with **no** `Authorization` header → **401**
- a request with a **forged** `initData` (valid shape, wrong signature) → **401**
- a request with a **correctly signed** `initData` → **200**, and the body naming the signing user
- `/webapp` → **200** and HTML

A guard your assertions need: 401 and 500 both mean "no data came back", so a route that 500s on
everything would satisfy a check that only asserts unauthenticated callers get nothing. The
binding assertion is that a correctly signed request returns **200 with the signing user's id in
the body** — that is the only one proving the whole chain works rather than failing closed.

Sign the good vector with the same helper from Task 8's check. The forged/valid pair is the assertion that matters: identical shape, different signature, opposite outcome.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
uv run python -c "import admin.app" 2>&1 | tail -1
git add bot/webapp/ bot/entrypoints/api.py bot/middlewares/metrics.py
git commit -m "feat(webapp): authenticated Mini App API with initData verification"
```

---

### Task 10: Demo page and launch surfaces

**Files:**
- Create: `bot/webapp/static/index.html`
- Modify: `bot/keyboards/inline/menu.py`, `bot/handlers/start.py`, `bot/handlers/menu.py`, `bot/handlers/callbacks.py`, `bot/core/config.py`, `.env.example`, `bot/locales/*`, `README.md`

**Interfaces:**
- Consumes: `GET /api/webapp/me`, `POST /api/webapp/invoice` (Task 9).
- Produces: a `WebAppInfo` launch button; `startapp` deep-link handling.

**Pre-verified:** `WebAppInfo(url=...)`; `InlineKeyboardButton.web_app`; `Message.web_app_data`; `Bot.answer_web_app_query` all exist in aiogram 3.30.

**Scope boundary:** one dependency-free page, no build step, no npm. It exists to prove the loop works end to end and to be replaced.

`README.md` is in scope (it is in the Modify list above). Add a short Mini App subsection — the
file's headings are emoji-prefixed `##`, match that style — stating plainly that the bundled page
is a dependency-free demo meant to be replaced; that Telegram will not load a Mini App over plain
HTTP and `localhost` will not work on mobile, so a tunnel is needed locally; and that the launch
button is omitted when `WEBAPP_URL` is unset. Add `WEBAPP_URL` to the existing
`## 🌍 Environment variables` section, matching whatever format that section already uses rather
than inventing a new one.

- [ ] **Step 1: Add the Mini App URL setting**

`WEBAPP_URL` in config and `.env.example`, with a comment noting Telegram **will not load a Mini App over plain HTTP**, and that `localhost` will not work on mobile — use a tunnel for local testing.

- [ ] **Step 2: Write `bot/webapp/static/index.html`**

It must satisfy all five checks the Mini App guidance calls out, each of which is a real failure mode:
1. `<script src="https://telegram.org/js/telegram-web-app.js"></script>` — without it `window.Telegram` is undefined.
2. `<meta name="viewport" content="width=device-width, initial-scale=1.0">` — without it the page renders desktop-width on phones.
3. `tg.ready()` on load — without it Telegram may keep showing its loading state.
4. Colours from the `--tg-theme-*` CSS variables — a page that ignores them looks foreign inside the client, and is unreadable in the theme it did not plan for.
5. `tg.MainButton` for the primary action rather than a custom button.

Behaviour: call `tg.ready()` and `tg.expand()`; `fetch('/api/webapp/me', {headers: {Authorization: 'tma ' + tg.initData}})` and render the returned name; wire `MainButton` to `POST /api/webapp/invoice` then `tg.openInvoice(link)`; fire `tg.HapticFeedback.impactOccurred('light')` on the purchase tap.

Send `tg.initData` — **never** `tg.initDataUnsafe`. The latter is the unsigned copy; sending it defeats Task 8 entirely.

- [ ] **Step 3: Add the launch button and deep link**

Add a `WebAppInfo` button to `main_keyboard()` when `WEBAPP_URL` is configured (omit it when unset — a `web_app` button with an empty URL is rejected by Telegram, the same class of bug as the existing `contacts_keyboard` issue recorded in the phase 1-6 log).

`main_keyboard()` currently takes **no arguments**, so this is not a one-file change. Follow the pattern `contacts_keyboard`/`support_keyboard` already establish in `bot/keyboards/inline/contacts.py`: the keyboard takes the URL as a parameter and the *handler* injects settings. Concretely:

```python
def main_keyboard(webapp_url: str | None = None) -> InlineKeyboardMarkup:
```

Do **not** call `get_settings()` inside the keyboard module — the phase 1-6 work removed import-time and hidden global settings access on purpose, and the established convention here is parameter-in.

There are exactly four call sites, all of which must pass the URL:
- `bot/handlers/start.py:14` — `start_handler` has no `settings`; add `settings: FromDishka[Settings]`.
- `bot/handlers/menu.py:13` — `menu_handler` has no `settings`; add `settings: FromDishka[Settings]`.
- `bot/handlers/callbacks.py:41` (`info_callback`) — has no `settings`; add it.
- `bot/handlers/callbacks.py:51` (`back_callback`) — has no `settings`; add it.

`support_callback` in the same file already injects `settings: FromDishka[Settings]` — copy its exact form. Both `start.py` and `menu.py` will need the `# ruff: noqa: TC001, TC002` header (or an existing one extended) once `Settings` appears in a runtime-resolved handler signature; check whether they already have it before adding a duplicate.

Handle `t.me/<bot>?startapp=<payload>`: it arrives as `/start <payload>`, which `find_command_argument` already extracts and `AuthMiddleware` already stores as `referrer`. Confirm that path works for `startapp` as well as `start`, and say so in your report — no new code should be needed.

- [ ] **Step 4: Verify the page and the button**

Fetch `/webapp` and assert the HTML contains the script tag, the viewport meta, `tg.ready(`,
`--tg-theme-`, `MainButton`, and `tg.initData` but **not** `initDataUnsafe`.

Careful with that last pair: a naive substring test for `tg.initData` **also matches inside
`tg.initDataUnsafe`**, so a page sending the unsigned copy would pass the check that exists
precisely to catch it. Assert the negative explicitly.

Then build `main_keyboard()` with and without `WEBAPP_URL` set. Assert the `web_app` button
appears in exactly one of them **and that the other keyboard still contains its four normal
buttons**. This is the contrast that matters: a `web_app` button with an empty URL is rejected by
Telegram and kills the *entire* keyboard, not just that button (the same class as the
`contacts_keyboard` defect in the phase 1-6 log), so a bug that drops everything when the URL is
missing would sail past a check that only asserts "no web_app button present".

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && git status --short
git add bot/webapp/static/ bot/keyboards/ bot/handlers/ bot/core/config.py .env.example bot/locales
git commit -m "feat(webapp): demo Mini App page and launch surfaces"
```

---

## Verification checklist for the whole plan

- [ ] `uv run ruff check . && uv run ruff format --check .` clean, `git diff` empty afterwards
- [ ] `uv run mypy` → **0 errors**
- [ ] `uv run python -c "import admin.app"` fails only on `OperationalError`
- [ ] No `tests/` directory exists
- [ ] `dp.resolve_used_update_types()` now includes `callback_query`, `pre_checkout_query` and `my_chat_member`
- [ ] Replaying one `SuccessfulPayment` twice creates exactly one `payments` row
- [ ] A forged `initData` returns 401; a correctly signed one returns 200
- [ ] `uv run alembic upgrade head` applies the payments migration cleanly on an empty database
- [ ] The worker boots with `payments:reconcile` registered

## Out of scope

Spec phases 10-14: the SQLAdmin migration, the notifier, Backupgram, ops (compose, pgbouncer transaction mode, **the Dockerfile CMD that currently makes `USE_WEBHOOK=True` refuse to start**), and docs. Each gets its own plan.

TON Connect is excluded by spec section 13 and is not in scope here.
