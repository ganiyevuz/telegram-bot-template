from aiogram import Router

from . import admin_payments, broadcast, callbacks, chat_member, export_users, info, menu, payments, start, support


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start.router)
    router.include_router(info.router)
    router.include_router(support.router)
    router.include_router(menu.router)
    router.include_router(export_users.router)
    router.include_router(broadcast.router)
    # admin_payments is command-filtered (Command("refund") + AdminFilter), so it is
    # not order-sensitive the way payments.router is below - see that comment.
    router.include_router(admin_payments.router)
    # payments must come before callbacks: MenuCB.filter(F.action == "premium") is
    # matched here, and aiogram stops at the first handler whose filters pass — if a
    # future edit adds a catch-all to callbacks.router, registration order decides
    # which handler owns "premium".
    router.include_router(payments.router)
    router.include_router(callbacks.router)
    router.include_router(chat_member.router)

    return router
