from __future__ import annotations
from typing import TYPE_CHECKING, Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_PER_PAGE = 5


class PageCallback(Protocol):
    """Builds the callback payload for a given item or navigation target."""

    def __call__(self, *, action: str, page: int) -> str: ...


def page_count(total: int, per_page: int = DEFAULT_PER_PAGE) -> int:
    if total <= 0:
        return 1
    return (total + per_page - 1) // per_page


def page_slice[T](items: Sequence[T], page: int, per_page: int = DEFAULT_PER_PAGE) -> list[T]:
    """Clamp `page` into range and return that page's items.

    Clamping rather than raising matters: a stale keyboard in an old message can
    send a page number that no longer exists, and a user tapping it should get
    the nearest valid page, not an error.
    """
    last = page_count(len(items), per_page) - 1
    page = max(0, min(page, last))
    start = page * per_page
    return list(items[start : start + per_page])


def paginate[T](
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
