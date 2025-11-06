from __future__ import annotations

import pytest
from datetime import date

from bot.schemas.onboarding import DailyPlan, OnboardingData, Gender, Goal, ActivityLevel
from bot.services.adjust import apply_adjustment, ParsedAdjustment


@pytest.mark.asyncio
async def test_single_macro_carbs_drop_goal_lose_calories_drop_no_fat_increase():
    # Base plan after onboarding
    base_plan = DailyPlan(
        calories=3590,
        protein_g=300,
        fat_g=110,
        carbs_g=350,
        sources=["onboarding"],
        tdee=3800,
        weekly_rate_kg=0.9,
        eta_date=date(2027, 10, 14),
    )
    data = OnboardingData(
        user_id=123,
        gender=Gender.male,
        age=30,
        weight_kg=100.0,
        height_cm=180.0,
        activity_text=None,
        activity_level=ActivityLevel.moderate,
        goal=Goal.lose,
        goal_weight_kg=91.0,
        speed=None,
    )

    parsed = ParsedAdjustment(
        intents=["low_carb", "custom_macros"],
        activity_override=None,
        calories=None,
        macros={
            "scheme": "custom",
            "custom_target_g": {"carbs_g": 270},
        },
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="test",
    )

    new_plan, explanation, summary = apply_adjustment(base_plan, data, parsed)

    assert new_plan.carbs_g == 270
    # Do not increase fat when goal is lose
    assert new_plan.fat_g <= base_plan.fat_g
    # Protein stays at least at base or above safety floor
    assert new_plan.protein_g >= 60
    # Calories drop proportionally (no compensation with fat): 300*4 + fat*9 + 270*4
    expected_cal = new_plan.protein_g * 4 + new_plan.fat_g * 9 + new_plan.carbs_g * 4
    assert new_plan.calories == expected_cal
    assert new_plan.calories < base_plan.calories


@pytest.mark.asyncio
async def test_reduce_fat_goal_lose_calories_drop_no_carb_compensation():
    base_plan = DailyPlan(
        calories=3000,
        protein_g=160,
        fat_g=90,
        carbs_g=300,
        sources=["onboarding"],
        tdee=3200,
        weekly_rate_kg=0.6,
        eta_date=None,
    )
    data = OnboardingData(
        user_id=123,
        gender=Gender.male,
        age=30,
        weight_kg=85.0,
        height_cm=180.0,
        activity_text=None,
        activity_level=ActivityLevel.light,
        goal=Goal.lose,
        goal_weight_kg=75.0,
        speed=None,
    )

    parsed = ParsedAdjustment(
        intents=["reduce_fat", "custom_macros"],
        activity_override=None,
        calories=None,
        macros={
            "scheme": "custom",
            "custom_target_g": {"fat_g": 70},
        },
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="test",
    )

    new_plan, explanation, summary = apply_adjustment(base_plan, data, parsed)

    assert new_plan.fat_g == 70
    # For lose goal we must not end up increasing fat
    assert new_plan.fat_g <= base_plan.fat_g
    # Calories drop by ~ 20g * 9 = 180 kcal (plus any safety adjustments on protein)
    assert new_plan.calories <= base_plan.calories - 9 * (base_plan.fat_g - new_plan.fat_g)
