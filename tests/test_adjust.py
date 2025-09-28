import pytest

from bot.schemas.onboarding import OnboardingData, DailyPlan, ActivityLevel, Goal
from bot.services.adjust import (
    parse_adjustment_heuristic,
    _apply_strength_defaults,
    apply_adjustment,
    ParsedAdjustment,
)


def _mk_data(
    *,
    user_id: int = 1,
    gender: str = "male",
    age: int = 30,
    weight_kg: float = 80.0,
    height_cm: float = 180.0,
    activity_text: str = "Хожу в зал 3 раза в неделю",
    activity_level: ActivityLevel | None = ActivityLevel.light,
    goal: Goal = Goal.lose,
    goal_weight_kg: float | None = 75.0,
):
    return OnboardingData(
        user_id=user_id,
        gender=gender,  # type: ignore[arg-type]
        age=age,
        weight_kg=weight_kg,
        height_cm=height_cm,
        activity_text=activity_text,
        activity_level=activity_level,
        goal=goal,
        goal_weight_kg=goal_weight_kg,
        speed=None,
    )


def _mk_plan(cal: int = 2000, p: int = 130, f: int = 60, c: int = 200) -> DailyPlan:
    return DailyPlan(
        calories=cal,
        protein_g=p,
        fat_g=f,
        carbs_g=c,
        sources=["unit-test"],
        tdee=2200,
        weekly_rate_kg=0.5,
        eta_date=None,
    )


def test_heuristic_percent_minus():
    text = "минус 10%"
    parsed = parse_adjustment_heuristic(text)
    assert parsed is not None
    assert parsed.calories and parsed.calories["mode"] == "percent"
    assert float(parsed.calories["value"]) == -10.0


def test_apply_strength_defaults_when_no_units_for_calories():
    # No explicit numbers -> should remain None here; strength defaults handled only when mode is None
    pa = ParsedAdjustment(
        intents=["raise_calories"],
        activity_override=None,
        calories={"mode": None, "value": None},
        macros=None,
        dietary_restrictions=[],
        confidence=1.0,
        rationale=None,
        version="t",
    )
    pa2 = _apply_strength_defaults(pa, text="хочу немного поднять калории")
    # In hybrid mode defaults apply turning None into percent by strength (slight -> +5% by default config)
    assert pa2.calories is not None
    assert pa2.calories["mode"] == "percent"
    assert float(pa2.calories["value"]) > 0


def test_apply_adjustment_raise_calories_delta_changes_numbers():
    data = _mk_data()
    base = _mk_plan(cal=1800, p=120, f=50, c=160)
    parsed = ParsedAdjustment(
        intents=["raise_calories"],
        activity_override=None,
        calories={"mode": "delta", "value": 200},
        macros=None,
        dietary_restrictions=[],
        confidence=0.9,
        rationale="Оставил без изменений.",
        version="v",
    )
    new_plan, explanation, summary = apply_adjustment(base, data, parsed)
    assert new_plan.calories == base.calories + 200
    # Explanation should be deterministic (not the LLM rationale) because numbers changed
    assert "Оставил без изменений" not in explanation
    assert str(new_plan.calories) in explanation


def test_apply_adjustment_activity_override_active_increases_calories():
    data = _mk_data(activity_level=ActivityLevel.light)
    base = _mk_plan(cal=1800)
    parsed = ParsedAdjustment(
        intents=["activity_up"],
        activity_override="active",
        calories=None,
        macros=None,
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="v",
    )
    new_plan, explanation, summary = apply_adjustment(base, data, parsed)
    # When only activity changed, calories baseline is recomputed from plan0 (higher than base)
    assert new_plan.calories >= base.calories
    assert any(x in explanation.lower() for x in ["активн", "высок"])
