
from bot.schemas.onboarding import ActivityLevel, DailyPlan, Goal, OnboardingData
from bot.services.adjust import ParsedAdjustment, apply_adjustment


def _mk_data(
    *,
    user_id: int = 1,
    gender: str = "male",
    age: int = 30,
    weight_kg: float = 80.0,
    height_cm: float = 180.0,
    activity_text: str = "Офисная работа",
    activity_level: ActivityLevel | None = ActivityLevel.moderate,
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


def _mk_plan(cal: int = 2000, p: int = 130, f: int = 60, c: int = 200, tdee: int = 2300) -> DailyPlan:
    return DailyPlan(
        calories=cal,
        protein_g=p,
        fat_g=f,
        carbs_g=c,
        sources=["unit-test"],
        tdee=tdee,
        weekly_rate_kg=0.5,
        eta_date=None,
    )


def test_llm_only_single_macro_percent_carbs_drop_calories() -> None:
    # Emulate LLM JSON for phrase like "уменьши углеводы на 10%"
    data = _mk_data(goal=Goal.lose)
    base = _mk_plan(cal=2000, p=120, f=60, c=200)
    # 10% off carbs -> 180g, single macro; calories should drop accordingly with others unchanged
    parsed = ParsedAdjustment(
        intents=["custom_macros"],
        activity_override=None,
        calories=None,
        macros={
            "scheme": "custom",
            "custom_target_g": {"carbs_g": 180, "protein_g": None, "fat_g": None},
        },
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="llm-only-mock",
    )
    new_plan, explanation, summary = apply_adjustment(base, data, parsed)
    assert new_plan.carbs_g == 180
    # Protein and fat preserved from base (since only one macro specified)
    assert new_plan.protein_g == base.protein_g
    assert new_plan.fat_g == base.fat_g
    # Calories should reduce vs base (no compensations)
    assert new_plan.calories < base.calories


def test_llm_only_faster_weight_loss_percent_calories() -> None:
    # Emulate LLM JSON for phrase like "хочу быстрее похудеть"
    data = _mk_data(goal=Goal.lose)
    base = _mk_plan(cal=2000, p=120, f=60, c=200)
    parsed = ParsedAdjustment(
        intents=["lower_calories"],
        activity_override=None,
        calories={"mode": "percent", "value": -10.0},
        macros=None,
        dietary_restrictions=[],
        confidence=0.9,
        rationale=None,
        version="llm-only-mock",
    )
    new_plan, explanation, summary = apply_adjustment(base, data, parsed)
    # Calorie target should be reduced by ~10% with safety clamps applied
    assert new_plan.calories < base.calories
    # Macros should be recomputed consistently with calorie change
    assert new_plan.protein_g > 0
    assert new_plan.fat_g > 0
    assert new_plan.carbs_g > 0
