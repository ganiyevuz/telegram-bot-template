from aiogram import Router

from . import broadcast, callbacks, export_users, info, menu, start, support


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start.router)
    router.include_router(info.router)
    router.include_router(support.router)
    router.include_router(menu.router)
    router.include_router(export_users.router)
    router.include_router(broadcast.router)
    router.include_router(callbacks.router)

    return router
