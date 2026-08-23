from aiogram import Router

from . import broadcast, callbacks, chat_member, export_users, info, menu, payments, start, support


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start.router)
    router.include_router(info.router)
    router.include_router(support.router)
    router.include_router(menu.router)
    router.include_router(export_users.router)
    router.include_router(broadcast.router)
    # payments must come before callbacks: MenuCB.filter(F.action == "premium") is
    # matched here, and aiogram stops at the first handler whose filters pass — if a
    # future edit adds a catch-all to callbacks.router, registration order decides
    # which handler owns "premium".
    router.include_router(payments.router)
    router.include_router(callbacks.router)
    router.include_router(chat_member.router)

    return router
