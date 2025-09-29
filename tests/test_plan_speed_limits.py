import math

from bot.schemas.onboarding import OnboardingData, ActivityLevel, Goal, Gender, Speed
from bot.services.plan import calculate_daily_plan


def _mk_data(
    *,
    user_id: int = 1,
    gender: Gender = Gender.male,
    age: int = 30,
    weight_kg: float = 80.0,
    height_cm: float = 180.0,
    activity_level: ActivityLevel = ActivityLevel.light,
    goal: Goal = Goal.lose,
    goal_weight_kg: float | None = None,
    speed: Speed | None = Speed.fast,
):
    return OnboardingData(
        user_id=user_id,
        gender=gender,
        age=age,
        weight_kg=weight_kg,
        height_cm=height_cm,
        activity_text=None,
        activity_level=activity_level,
        goal=goal,
        goal_weight_kg=goal_weight_kg,
        speed=speed,
    )


def test_lose_capped_by_frac_deficit():
    # Case: requested deficit slightly above 35% TDEE => cap by fractional limit (0.35)
    # Setup: 120 kg, 180 cm, male, light activity
    data = _mk_data(weight_kg=120.0, height_cm=180.0, activity_level=ActivityLevel.light, goal=Goal.lose, speed=Speed.fast)
    plan = calculate_daily_plan(data)

    # BMR = 10*w + 6.25*h - 5*age + 5 = 10*120 + 6.25*180 - 150 + 5 = 2180
    bmr = 2180.0
    tdee = bmr * 1.375  # light
    frac_cap = 0.35 * tdee

    # Requested deficit for FAST: 0.8% bw/week => 0.96 kg/wk
    requested_deficit = 0.96 * 7700.0 / 7.0  # ≈ 1056

    # Binding cap should be fractional (min(frac_cap, 1200))
    expected_deficit = min(frac_cap, 1200.0)
    assert expected_deficit < requested_deficit

    # Target calories = TDEE - expected_deficit (floats rounded at the end by code)
    expected_target = tdee - expected_deficit
    assert abs(plan.tdee - round(tdee)) <= 2
    assert abs(plan.calories - int(round(expected_target))) <= 2


def test_lose_capped_by_abs_deficit():
    # Case: requested deficit above 1200 and frac 35% * TDEE is higher than 1200 => cap by absolute 1200
    # Setup: 140 kg, 190 cm, male, active activity (high TDEE)
    data = _mk_data(
        weight_kg=140.0,
        height_cm=190.0,
        activity_level=ActivityLevel.active,
        goal=Goal.lose,
        speed=Speed.fast,
    )
    plan = calculate_daily_plan(data)

    # Rough checks
    # BMR ≈ 2442.5; TDEE ≈ 2442.5 * 1.725 ≈ 4213
    # Requested deficit: 1.12 kg/wk => ~1232 kcal/day
    # frac_cap = 0.35 * 4213 ≈ 1475 (> 1200) => absolute 1200 should bind
    assert plan.tdee > 3800
    # target should be roughly TDEE - 1200
    assert abs((plan.tdee - plan.calories) - 1200) <= 30


def test_gain_capped_by_surplus_and_rate():
    # Gain: FAST requests 0.8%/wk, but MAX_GAIN_RATE=0.7% and surplus capped at 600 kcal/day.
    # Setup: 80 kg, moderate activity, maintain->gain with FAST
    data = _mk_data(
        weight_kg=80.0,
        height_cm=180.0,
        activity_level=ActivityLevel.moderate,
        goal=Goal.gain,
        speed=Speed.fast,
    )
    plan = calculate_daily_plan(data)

    # Expect surplus capped to 600 kcal/day
    assert plan.calories - plan.tdee <= 620
    assert plan.calories - plan.tdee >= 580

    # weekly_rate_kg should align with surplus: rate = surplus * 7 / 7700
    expected_rate = (plan.calories - plan.tdee) * 7.0 / 7700.0
    assert math.isclose(plan.weekly_rate_kg, round(expected_rate, 2), rel_tol=0.05, abs_tol=0.05)
