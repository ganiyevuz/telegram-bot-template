from __future__ import annotations
from typing import TYPE_CHECKING

from aiogram.types import BotCommand, BotCommandScopeDefault
from aiogram.utils.i18n import lazy_gettext as __

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.utils.i18n.core import I18n
    from aiogram.utils.i18n.lazy_proxy import LazyProxy

# Descriptions are `lazy_gettext` proxies: `set_default_commands` runs at
# startup with no per-user context, so these can only be resolved by pushing
# an explicit locale via `I18n.use_locale()` per iteration below - see there.
users_commands: dict[str, LazyProxy] = {
    "help": __("help"),
    "contacts": __("developer contact details"),
    "menu": __("main menu"),
    "settings": __("your settings"),
    "supports": __("support contacts"),
}

admins_commands: dict[str, dict[str, str]] = {
    "en": {
        "ping": "Check bot ping",
        "stats": "Show bot stats",
    },
    "uk": {
        "ping": "Check bot ping",
        "stats": "Show bot stats",
    },
    "ru": {
        "ping": "Check bot ping",
        "stats": "Show bot stats",
    },
}


async def set_default_commands(bot: Bot, i18n: I18n) -> None:
    await remove_default_commands(bot)

    for locale in i18n.available_locales:
        # `lazy_gettext` proxies resolve against whatever locale is current on
        # `I18n`'s contextvar; `context()` makes this instance the one that
        # `get_i18n()` returns, and `use_locale()` sets that locale for the
        # `str(proxy)` calls in the comprehension below. Same pattern aiogram's
        # own `I18nMiddleware.__call__` uses per-request.
        with i18n.context(), i18n.use_locale(locale):
            commands = [
                BotCommand(command=command, description=str(description))
                for command, description in users_commands.items()
            ]

        await bot.set_my_commands(
            commands,
            scope=BotCommandScopeDefault(),
            language_code=locale,
        )

        """ Commands for admins
        for admin_id in await admin_ids():
            await bot.set_my_commands(
                [
                    BotCommand(command=command, description=description)
                    for command, description in admins_commands[language_code].items()
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        """


async def remove_default_commands(bot: Bot) -> None:
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
