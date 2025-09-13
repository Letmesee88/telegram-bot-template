from aiogram import Router

from . import admin, export_users, onboarding, start, foodai


def get_handlers_router() -> Router:
    router = Router()
    # Important: include FoodAI before broad text handlers (e.g., start) so it can handle meal text
    router.include_router(foodai.router)
    # Include edit-state router after main FoodAI router
    router.include_router(foodai.router_edit)
    router.include_router(onboarding.router)
    router.include_router(start.router)
    router.include_router(admin.router)
    router.include_router(export_users.router)

    return router
