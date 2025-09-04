from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import UserModel


class FoodAIEnabledFilter(BaseFilter):
    """Allows only users with FoodAI feature enabled (users.foodai_enabled_at IS NOT NULL)."""

    async def __call__(self, event: Message | CallbackQuery, session: AsyncSession) -> bool:
        user = None
        if isinstance(event, (Message, CallbackQuery)):
            user = event.from_user
        if not user:
            return False

        db_user = await session.get(UserModel, user.id)
        if not db_user:
            return False

        return db_user.foodai_enabled_at is not None
