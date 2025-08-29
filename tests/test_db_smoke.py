from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models.user import UserModel


@pytest.mark.asyncio
async def test_migrations_applied(db_session: AsyncSession) -> None:
    # Ensure table exists by running a trivial query
    res = await db_session.execute(text("SELECT to_regclass('public.users')"))
    assert res.scalar() == "users"


@pytest.mark.asyncio
async def test_user_insert_and_select(db_session: AsyncSession) -> None:
    user = UserModel(
        id=999999999,
        first_name="Test",
        last_name=None,
        username=None,
        language_code="ru",
        referrer=None,
        is_admin=False,
        is_suspicious=False,
        is_block=False,
        is_premium=False,
    )
    db_session.add(user)
    await db_session.commit()

    q = select(UserModel).where(UserModel.id == user.id)
    res = await db_session.execute(q)
    got = res.scalar_one()

    assert got.first_name == "Test"
