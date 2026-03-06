from __future__ import annotations

import pytest
from sqlalchemy import text, update

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, WeightLogModel
from bot.services.weight import build_history_page, compute_progress, save_weight


@pytest.mark.asyncio
async def test_compute_progress_gain_and_lose_and_clipping() -> None:
    # Lose: start 100 -> goal 80
    assert compute_progress(100.0, 100.0, 80.0) == 0.0
    assert compute_progress(100.0, 90.0, 80.0) == 50.0
    assert compute_progress(100.0, 80.0, 80.0) == 100.0
    assert compute_progress(100.0, 70.0, 80.0) == 100.0  # clipping

    # Gain: start 60 -> goal 80
    assert compute_progress(60.0, 60.0, 80.0) == 0.0
    assert compute_progress(60.0, 70.0, 80.0) == 50.0
    assert compute_progress(60.0, 80.0, 80.0) == 100.0
    assert compute_progress(60.0, 90.0, 80.0) == 100.0  # clipping

    # Edge: missing values
    assert compute_progress(None, 70.0, 80.0) is None
    assert compute_progress(70.0, None, 80.0) is None
    assert compute_progress(70.0, 75.0, None) is None
    assert compute_progress(70.0, 75.0, 70.0) is None


@pytest.mark.asyncio
async def test_history_pagination_and_date_format(ensure_user) -> None:
    user_id = await ensure_user(user_id=23001)

    # Provide onboarding goal to avoid missing fields in UI elsewhere
    async with sessionmaker() as session:
        oa = OnboardingAnswerModel(
            user_id=user_id,
            data={"goal_weight_kg": 80.0},
            daily_plan={},
            goal="lose",
            calories=0,
        )
        session.add(oa)
        await session.commit()

    # Generate 25 entries by saving and then shifting recorded_at back by i days
    for i in range(25):
        val = 100.0 - i
        v, d = await save_weight(user_id, val)
        # Shift recorded_at and recorded_local_date back by i days to simulate historical entries
        async with sessionmaker() as session:
            await session.execute(
                update(WeightLogModel)
                .where((WeightLogModel.user_id == user_id) & (WeightLogModel.recorded_local_date == d))
                .values(
                    recorded_at=(WeightLogModel.recorded_at - text("INTERVAL '%d day'" % i)),
                    recorded_local_date=(WeightLogModel.recorded_local_date - text("INTERVAL '%d day'" % i)),
                )
            )
            await session.commit()

    # Page 1 (page_size=10)
    page1 = await build_history_page(user_id, page=1, page_size=10)
    assert page1.page == 1
    assert page1.has_prev is False
    assert page1.has_next is True
    lines1 = page1.text.splitlines()
    assert lines1[0].startswith("📉 История моего веса")
    # Ensure date format dd.mm.yyyy appears (line like "• 31.10.2025: 75.0 кг")
    assert any(": " in ln and " кг" in ln for ln in lines1[1:])

    # Page 2 (10 more)
    page2 = await build_history_page(user_id, page=2, page_size=10)
    assert page2.page == 2
    assert page2.has_prev is True
    assert page2.has_next is True
    # Page 3 (remaining 5)
    page3 = await build_history_page(user_id, page=3, page_size=10)
    assert page3.page == 3
    assert page3.has_prev is True
    assert page3.has_next is False
    lines3 = page3.text.splitlines()
    assert len([ln for ln in lines3 if ln.startswith("• ")]) <= 5
