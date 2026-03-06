from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.users import is_admin


class AdminFilter(BaseFilter):
    """Allows only administrators (whose database column is_admin=True)."""

    async def __call__(self, event: Message | CallbackQuery, session: AsyncSession) -> bool:
        user = None
        if isinstance(event, (CallbackQuery, Message)):
            user = event.from_user

        if not user:
            return False

        return await is_admin(session=session, user_id=user.id)
