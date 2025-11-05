from __future__ import annotations

import pytest

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel
from bot.services.account import get_account_summary_text
from bot.services.weight import save_weight


@pytest.mark.asyncio
async def test_account_summary_uses_log_current_and_no_progress_line(ensure_user):
    user_id = await ensure_user(user_id=21001)

    # Setup onboarding: start=100, goal=80, current in onboarding=100
    async with sessionmaker() as session:
        oa = OnboardingAnswerModel(
            user_id=user_id,
            data={
                "weight_kg": 100.0,
                "start_weight_kg": 100.0,
                "goal_weight_kg": 80.0,
            },
            daily_plan={"calories": 2000},
            goal="lose",
            calories=2000,
        )
        session.add(oa)
        await session.commit()

    # Save today's weight in logs -> current should come from logs
    await save_weight(user_id, 90.0)

    text = await get_account_summary_text(user_id)

    assert "⚖️ Контроль веса" in text
    assert "Текущий: 90.0 кг → Цель: 80.0 кг" in text
    assert "Осталось: 10.0 кг" in text
    assert "Прогресс" not in text


@pytest.mark.asyncio
async def test_account_summary_no_logs_fallbacks_to_onboarding_weight(ensure_user):
    user_id = await ensure_user(user_id=21002)

    # Onboarding only, no logs
    async with sessionmaker() as session:
        oa = OnboardingAnswerModel(
            user_id=user_id,
            data={
                "weight_kg": 95.0,
                "start_weight_kg": 100.0,
                "goal_weight_kg": 80.0,
            },
            daily_plan={"calories": 0},
            goal="lose",
            calories=0,
        )
        session.add(oa)
        await session.commit()

    text = await get_account_summary_text(user_id)

    assert "Текущий: 95.0 кг → Цель: 80.0 кг" in text
    assert "Осталось: 15.0 кг" in text
    assert "Прогресс" not in text
