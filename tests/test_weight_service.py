from __future__ import annotations
from datetime import date

import pytest
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, WeightLogModel
from bot.services.weight import build_my_weight_text, get_current_weight, save_weight


@pytest.mark.asyncio
async def test_save_weight_upsert_and_current(ensure_user) -> None:
    user_id = await ensure_user(user_id=20001)

    # First save today
    v1, d1 = await save_weight(user_id, 80.0)
    assert pytest.approx(v1, rel=1e-6) == 80.0
    assert isinstance(d1, date)

    cur = await get_current_weight(user_id)
    assert cur == 80.0

    # Second save same local day -> upsert
    v2, d2 = await save_weight(user_id, 79.5)
    assert d2 == d1
    assert v2 == 79.5

    cur2 = await get_current_weight(user_id)
    assert cur2 == 79.5

    # Ensure only one row exists for that local date
    async with sessionmaker() as session:
        rows = list((await session.execute(
            select(WeightLogModel).where(
                (WeightLogModel.user_id == user_id) & (WeightLogModel.recorded_local_date == d1)
            )
        )).scalars().all())
    assert len(rows) == 1
    assert float(rows[0].weight_kg) == 79.5


@pytest.mark.asyncio
async def test_build_my_weight_text_no_progress_line(ensure_user) -> None:
    user_id = await ensure_user(user_id=20002)

    # Onboarding goal and start
    async with sessionmaker() as session:
        oa = OnboardingAnswerModel(
            user_id=user_id,
            data={
                "weight_kg": 100.0,
                "start_weight_kg": 100.0,
                "goal_weight_kg": 80.0,
            },
            daily_plan={},
            goal="lose",
            calories=0,
        )
        session.add(oa)
        await session.commit()

    # Current via logs
    await save_weight(user_id, 90.0)

    text = await build_my_weight_text(user_id)
    # Must include current/goal and only "До цели", without progress
    assert "• Текущий вес: 90.0 кг" in text
    assert "• Целевой вес: 80.0 кг" in text
    assert "• До цели: 10.0 кг" in text
    assert "Прогресс" not in text
