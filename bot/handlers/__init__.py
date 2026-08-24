from aiogram import Router

from . import (
    admin_backup,
    admin_payments,
    broadcast,
    callbacks,
    chat_member,
    export_users,
    info,
    menu,
    payments,
    start,
    support,
)


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start.router)
    router.include_router(info.router)
    router.include_router(support.router)
    router.include_router(menu.router)
    router.include_router(export_users.router)
    router.include_router(broadcast.router)
    # admin_payments and admin_backup are command-filtered (Command(...) + AdminFilter), so
    # they are not order-sensitive the way payments.router is below - see that comment.
    router.include_router(admin_payments.router)
    router.include_router(admin_backup.router)
    # payments must come before callbacks: MenuCB.filter(F.action == "premium") is
    # matched here, and aiogram stops at the first handler whose filters pass — if a
    # future edit adds a catch-all to callbacks.router, registration order decides
    # which handler owns "premium".
    router.include_router(payments.router)
    router.include_router(callbacks.router)
    router.include_router(chat_member.router)

    return router
