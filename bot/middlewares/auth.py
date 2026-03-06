from __future__ import annotations
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from loguru import logger

from bot.services.users import add_user, user_exists
from bot.utils.command import find_command_argument

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject
    from sqlalchemy.ext.asyncio import AsyncSession


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession = data["session"]

        user = None
        referrer: str | None = None

        if isinstance(event, Message):
            user = event.from_user
            referrer = find_command_argument(event.text)
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            # referrer не извлекаем из callback
        else:
            return await handler(event, data)

        if not user:
            return await handler(event, data)

        if await user_exists(session, user.id):
            return await handler(event, data)

        logger.info(f"new user registration | user_id: {user.id}")

        await add_user(session=session, user=user, referrer=referrer)

        return await handler(event, data)
