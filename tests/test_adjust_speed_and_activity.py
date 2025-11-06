from __future__ import annotations

from datetime import date, timedelta

import pytest

from bot.schemas.onboarding import DailyPlan, OnboardingData, Gender, Goal, ActivityLevel
from bot.services.adjust import apply_adjustment, ParsedAdjustment
from bot.services.plan import MAX_DEFICIT_ABS, MAX_DEFICIT_FRAC


def make_data(weight: float = 90.0, height: float = 180.0,
              activity: ActivityLevel = ActivityLevel.moderate,
              goal: Goal = Goal.lose,
              goal_weight: float = 80.0) -> OnboardingData:
    return OnboardingData(
        user_id=101,
        gender=Gender.male,
        age=30,
        weight_kg=weight,
        height_cm=height,
        activity_text=None,
        activity_level=activity,
        goal=goal,
        goal_weight_kg=goal_weight,
        speed=None,
    )


def make_base_plan(cal: int = 3000, p: int = 180, f: int = 90, c: int = 300,
                   tdee: int = 3200) -> DailyPlan:
    return DailyPlan(
        calories=cal,
        protein_g=p,
        fat_g=f,
        carbs_g=c,
        sources=["onboarding"],
        tdee=tdee,
        weekly_rate_kg=0.5,
        eta_date=date.today(),
    )


@pytest.mark.asyncio
async def test_rate_per_week_applies_expected_calories_lose():
    data = make_data(weight=100.0, activity=ActivityLevel.moderate, goal=Goal.lose, goal_weight=90.0)
    base_plan = make_base_plan(cal=3200, p=220, f=90, c=300, tdee=3300)

    parsed = ParsedAdjustment(
        intents=["lower_calories"],
        activity_override=None,
        calories={"mode": "rate_per_week", "value": 0.5},  # 0.5 kg/week
        macros=None,
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="test",
    )

    new_plan, explanation, summary = apply_adjustment(base_plan, data, parsed)

    # Expected target calories = clamp(tdee - delta), where delta = 0.5*7700/7
    delta = 0.5 * 7700.0 / 7.0
    expected = int(round(new_plan.tdee - delta))
    assert new_plan.calories == expected
    assert new_plan.calories < base_plan.calories


@pytest.mark.asyncio
async def test_deadline_date_applies_expected_calories_lose():
    data = make_data(weight=95.0, goal=Goal.lose, goal_weight=85.0)
    base_plan = make_base_plan(cal=2900, p=200, f=80, c=300, tdee=3000)

    # 70 days to deadline
    deadline = (date.today() + timedelta(days=70)).isoformat()
    parsed = ParsedAdjustment(
        intents=["lower_calories"],
        activity_override=None,
        calories={"mode": "deadline", "value": deadline},
        macros=None,
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="test",
    )

    new_plan, explanation, summary = apply_adjustment(base_plan, data, parsed)

    weeks = 70 / 7.0
    kg_left = abs(data.weight_kg - data.goal_weight_kg)
    rate = kg_left / weeks
    delta = rate * 7700.0 / 7.0
    # Apply the same clamping as backend
    tdee = float(new_plan.tdee)
    raw = tdee - delta
    max_def = min(MAX_DEFICIT_ABS, MAX_DEFICIT_FRAC * tdee)
    min_allowed = max(100.0, tdee - max_def)
    expected = int(round(max(min_allowed, min(raw, tdee))))
    assert new_plan.calories == expected
    assert new_plan.calories < base_plan.calories


@pytest.mark.asyncio
async def test_activity_override_changes_tdee_not_calories():
    data = make_data(weight=85.0, activity=ActivityLevel.moderate)
    base_plan = make_base_plan(cal=2600, p=180, f=70, c=250, tdee=2700)

    parsed = ParsedAdjustment(
        intents=["activity_down"],
        activity_override="sedentary",
        calories=None,
        macros=None,
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="test",
    )

    new_plan, explanation, summary = apply_adjustment(base_plan, data, parsed)

    # Calories should remain unchanged; TDEE should decrease due to sedentary
    assert new_plan.calories == base_plan.calories
    assert new_plan.tdee < base_plan.tdee
