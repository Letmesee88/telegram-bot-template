from aiogram import Router

from . import admin, export_users, onboarding, start, foodai, menu
from . import history
from . import templates
from . import recommendations
from . import account
from . import weight


def get_handlers_router() -> Router:
    router = Router()
    # Recommendations router first to ensure its callbacks are registered
    router.include_router(recommendations.router)
    # Important: include FoodAI before broad text handlers (e.g., start) so it can handle meal text
    router.include_router(foodai.router)
    # Include edit-state router after main FoodAI router
    router.include_router(foodai.router_edit)
    # Templates router for browsing/applying meal templates
    router.include_router(templates.router)
    router.include_router(onboarding.router)
    router.include_router(menu.router)
    router.include_router(history.router)
    router.include_router(account.router)
    router.include_router(weight.router)
    router.include_router(start.router)
    router.include_router(admin.router)
    router.include_router(export_users.router)

    return router
