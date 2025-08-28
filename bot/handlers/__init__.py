from aiogram import Router

from . import admin, export_users, onboarding, start


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start.router)
    router.include_router(admin.router)
    router.include_router(export_users.router)
    router.include_router(onboarding.router)

    return router
