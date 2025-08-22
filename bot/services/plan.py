from __future__ import annotations

from math import floor
from typing import Tuple

from loguru import logger

from bot.schemas.onboarding import (
    ACTIVITY_MULTIPLIERS,
    ActivityLevel,
    DailyPlan,
    Gender,
    Goal,
    OnboardingData,
    Speed,
)

# Sources required by TZ
SOURCES = [
    "https://pubmed.ncbi.nlm.nih.gov/2305711/",  # Mifflin-St Jeor (1990)
    "https://journals.physiology.org/doi/full/10.1152/ajpendo.00156.2017",  # Metabolic calculations
    "https://ceur-ws.org/Vol-3806/S_42_Pleskach.pdf",  # Calorie counting systems
]


def calc_bmr_mifflin(gender: Gender, age: int, weight: float, height: float) -> float:
    """Basal Metabolic Rate (Mifflin–St Jeor).

    weight: kg, height: cm, age: years
    """
    bmr = 10 * weight + 6.25 * height - 5 * age
    if gender == Gender.male:
        bmr += 5
    else:
        bmr -= 161
    return float(bmr)


def _infer_activity_level(text: str) -> ActivityLevel:
    """Very simple heuristic from free-form text to activity level.
    Minimal level is sedentary per TZ.
    """
    t = text.lower()
    # Strongest first
    if any(k in t for k in ["соревн", "атлет", "проф", "ежедневные тренировки", "2 раза в день"]):
        return ActivityLevel.athlete
    if any(k in t for k in ["каждый день", "ежедневно трен", "6 раз в неделю", "интенсивные трен", "кроссфит"]):
        return ActivityLevel.active
    if any(k in t for k in ["3", "3 раза", "зал", "силов", "бег", "кардио", "вел", "плаван", "футбол", "спортзал"]):
        return ActivityLevel.moderate
    if any(k in t for k in ["1-2", "1–2", "1 раз", "2 раза", "йога", "пилатес", "прогулк", "шаг", "пешком", "ходьб"]):
        return ActivityLevel.light
    if any(k in t for k in ["сидяч", "офис", "компьютер", "мало двигаюсь", "без активности"]):
        return ActivityLevel.sedentary
    # Default minimal
    return ActivityLevel.sedentary


def _apply_goal_and_speed(tdee: float, goal: Goal, speed: Speed | None) -> float:
    """Apply base goal adjustment and speed multiplier as percent of TDEE.
    Base: lose -15%, gain +15%, maintain 0%.
    Speed: COMFORT ±5%, EFFORT ±10%, FAST ±15% (same direction), maintain ignores speed.
    """
    base = 0.0
    if goal == Goal.lose:
        base = -0.15
    elif goal == Goal.gain:
        base = 0.15
    else:  # maintain
        base = 0.0

    extra = 0.0
    if goal != Goal.maintain and speed is not None:
        if speed == Speed.comfort:
            extra = 0.05 if goal == Goal.gain else -0.05
        elif speed == Speed.effort:
            extra = 0.10 if goal == Goal.gain else -0.10
        elif speed == Speed.fast:
            extra = 0.15 if goal == Goal.gain else -0.15

    factor = 1.0 + base + extra
    return max(100.0, tdee * factor)  # never below 100 kcal as a sanity floor


def _macro_split(goal: Goal) -> Tuple[float, float, float]:
    """Return protein, fat, carb fractions for calories (sum to 1.0)."""
    if goal == Goal.lose:
        return 0.35, 0.25, 0.40
    if goal == Goal.gain:
        return 0.30, 0.30, 0.40
    return 0.30, 0.30, 0.40  # maintain


def _cal_to_grams(calories: float, p_frac: float, f_frac: float, c_frac: float) -> Tuple[int, int, int]:
    p_cal = calories * p_frac
    f_cal = calories * f_frac
    c_cal = calories * c_frac
    # 4/9/4 kcal per gram
    p_g = round(p_cal / 4)
    f_g = round(f_cal / 9)
    c_g = round(c_cal / 4)
    return int(p_g), int(f_g), int(c_g)


def calculate_daily_plan(data: OnboardingData) -> DailyPlan:
    """Main entry: compute daily calories and macros based on onboarding answers.

    Steps:
    - BMR via Mifflin–St Jeor
    - Determine activity level (provided or inferred)
    - TDEE = BMR * activity_multiplier
    - Apply goal and speed adjustments
    - Split macros per TZ and convert to grams
    """
    # Activity
    activity_level = data.activity_level or _infer_activity_level(data.activity_text)
    activity_multiplier = ACTIVITY_MULTIPLIERS[activity_level]

    # BMR and TDEE
    bmr = calc_bmr_mifflin(data.gender, data.age, data.weight_kg, data.height_cm)
    tdee = bmr * activity_multiplier

    # Apply goal/speed
    target_cal = _apply_goal_and_speed(tdee, data.goal, data.speed)

    # Macros
    p_frac, f_frac, c_frac = _macro_split(data.goal)
    p_g, f_g, c_g = _cal_to_grams(target_cal, p_frac, f_frac, c_frac)

    result = DailyPlan(
        calories=int(round(target_cal)),
        protein_g=p_g,
        fat_g=f_g,
        carbs_g=c_g,
        sources=SOURCES,
    )

    logger.info(
        "daily_plan computed | user_id={}, gender={}, age={}, w={}, h={}, act={}, bmr={}, tdee={}, target={}, p/f/c={}/{}/{}",
        data.user_id,
        data.gender,
        data.age,
        data.weight_kg,
        data.height_cm,
        activity_level.value,
        round(bmr, 2),
        round(tdee, 2),
        result.calories,
        result.protein_g,
        result.fat_g,
        result.carbs_g,
    )

    return result
