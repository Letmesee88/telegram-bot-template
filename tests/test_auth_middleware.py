from __future__ import annotations
from typing import TYPE_CHECKING

import pytest
from aiogram import types
from sqlalchemy import select

from bot.database.models.user import UserModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_user_dict(user_id: int = 12345) -> dict:
    return {
        "id": user_id,
        "is_bot": False,
        "first_name": "Test",
        "last_name": None,
        "username": None,
        "language_code": "ru",
        "is_premium": False,
    }


def _make_chat_dict(chat_id: int) -> dict:
    return {
        "id": chat_id,
        "type": "private",
    }


def _make_message(user_id: int = 12345, text: str = "/start") -> types.Message:
    payload = {
        "message_id": 1,
        "date": 0,
        "chat": _make_chat_dict(user_id),
        "from": _make_user_dict(user_id),
        "text": text,
    }
    return types.Message.model_validate(payload)


def _make_callback(data: str, user_id: int = 12345) -> types.CallbackQuery:
    msg = _make_message(user_id, text="stub")
    payload = {
        "id": "1",
        "from": _make_user_dict(user_id),
        "message": msg.model_dump(by_alias=True),
        "chat_instance": "ci",
        "data": data,
    }
    return types.CallbackQuery.model_validate(payload)


@pytest.mark.asyncio
async def test_auth_middleware_registers_user_on_message(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch functions inside auth middleware to avoid Redis usage
    from bot.middlewares import auth as auth_mw

    async def _user_exists(session: AsyncSession, user_id: int) -> bool:
        res = await session.execute(select(UserModel.id).where(UserModel.id == user_id))
        return res.scalar_one_or_none() is not None

    async def _add_user(session: AsyncSession, user: types.User, referrer: str | None) -> None:
        obj = UserModel(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            language_code=user.language_code,
            is_premium=bool(user.is_premium or False),
            referrer=referrer,
        )
        session.add(obj)
        await session.commit()

    monkeypatch.setattr(auth_mw, "user_exists", _user_exists)
    monkeypatch.setattr(auth_mw, "add_user", _add_user)

    event = _make_message(user_id=777, text="/start")

    async def _handler(_event, data) -> str:
        return "ok"

    mw = auth_mw.AuthMiddleware()
    data: dict = {"session": db_session}

    await mw(_handler, event, data)

    # Verify user persisted
    res = await db_session.execute(select(UserModel).where(UserModel.id == 777))
    assert res.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_auth_middleware_registers_user_on_callback(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.middlewares import auth as auth_mw

    async def _user_exists(session: AsyncSession, user_id: int) -> bool:
        res = await session.execute(select(UserModel.id).where(UserModel.id == user_id))
        return res.scalar_one_or_none() is not None

    async def _add_user(session: AsyncSession, user: types.User, referrer: str | None) -> None:
        obj = UserModel(
            id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
            language_code=user.language_code,
            is_premium=bool(user.is_premium or False),
            referrer=referrer,
        )
        session.add(obj)
        await session.commit()

    monkeypatch.setattr(auth_mw, "user_exists", _user_exists)
    monkeypatch.setattr(auth_mw, "add_user", _add_user)

    event = _make_callback(data="start:no", user_id=888)

    async def _handler(_event, data) -> str:
        return "ok"

    mw = auth_mw.AuthMiddleware()
    data: dict = {"session": db_session}

    await mw(_handler, event, data)

    res = await db_session.execute(select(UserModel).where(UserModel.id == 888))
    assert res.scalar_one_or_none() is not None
