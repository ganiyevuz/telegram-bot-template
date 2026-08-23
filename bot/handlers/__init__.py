from aiogram import Router

from . import broadcast, callbacks, chat_member, export_users, info, menu, start, support


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start.router)
    router.include_router(info.router)
    router.include_router(support.router)
    router.include_router(menu.router)
    router.include_router(export_users.router)
    router.include_router(broadcast.router)
    router.include_router(callbacks.router)
    router.include_router(chat_member.router)

    return router
