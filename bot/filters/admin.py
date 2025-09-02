from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.users import is_admin


class AdminFilter(BaseFilter):
    """Allows only administrators (whose database column is_admin=True)."""

    async def __call__(self, event: Message | CallbackQuery, session: AsyncSession) -> bool:
        user = None
        if isinstance(event, CallbackQuery):
            user = event.from_user
        elif isinstance(event, Message):
            user = event.from_user

        if not user:
            return False

        return await is_admin(session=session, user_id=user.id)
