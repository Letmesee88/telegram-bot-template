from __future__ import annotations
import re
from datetime import date, timedelta

from loguru import logger

from bot.core.config import settings
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

# Speed presets as percent of body weight per week (absolute value)
SPEED_PERCENT_BY_WEIGHT = {
    Speed.comfort: 0.003,  # 0.3%
    Speed.effort: 0.005,   # 0.5%
    Speed.fast: 0.008,     # 0.8%
}

# Safety limits (configurable via settings)
MAX_LOSS_RATE = float(getattr(settings, "PLAN_MAX_LOSS_RATE", 0.01))
MAX_GAIN_RATE = float(getattr(settings, "PLAN_MAX_GAIN_RATE", 0.005))
MAX_DEFICIT_ABS = int(getattr(settings, "PLAN_MAX_DEFICIT_ABS", 1000))
MAX_DEFICIT_FRAC = float(getattr(settings, "PLAN_MAX_DEFICIT_FRAC", 0.30))
GAIN_MIN_SURPLUS = int(getattr(settings, "PLAN_GAIN_MIN_SURPLUS", 200))
GAIN_MAX_SURPLUS = int(getattr(settings, "PLAN_GAIN_MAX_SURPLUS", 500))


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
    t = (text or "").lower()

    # Strongest first: explicit athlete signals
    if any(k in t for k in ["соревн", "атлет", "проф", "ежедневные тренировки", "2 раза в день"]):
        return ActivityLevel.athlete

    # Extract workouts per week like "5 раз в неделю", "4 раза/нед", supports ranges "5–6"
    wpw = None
    m = re.search(r"(\d+)[\s\-–]*?(?:раз|раза)[^\n]{0,12}?(?:недел[ьяи]|нед)", t)
    if m:
        try:
            wpw = int(m.group(1))
        except Exception:
            wpw = None
    # Treat "каждый день" as >=6/нед
    if wpw is None and ("каждый день" in t or "ежедневно" in t):
        wpw = 6

    # Martial arts / high-intensity sports signals
    combat = any(k in t for k in [
        "единоборств", "единоборства", "борьб", "бокс", "кикбокс", "тайский бокс", "муай тай", "дdddd", "джиу", "самбо", "каратэ", "mma"
    ])

    # Active tier by frequency
    if isinstance(wpw, int):
        if wpw >= 6:
            return ActivityLevel.active
        if wpw >= 4:
            return ActivityLevel.active
        if wpw >= 3:
            return ActivityLevel.moderate
        if wpw >= 1:
            return ActivityLevel.light

    # Keyword-based fallbacks
    if any(k in t for k in ["интенсивные трен", "кроссфит"]):
        return ActivityLevel.active
    if any(k in t for k in ["зал", "силов", "бег", "кардио", "вел", "плаван", "футбол", "спортзал", "танц"]):
        # If combat sports mentioned without frequency, lean to moderate
        return ActivityLevel.moderate if (combat or "бег" in t or "зал" in t) else ActivityLevel.light
    if any(k in t for k in ["йога", "пилатес", "прогулк", "шаг", "пешком", "ходьб"]):
        return ActivityLevel.light
    if any(k in t for k in ["сидяч", "офис", "компьютер", "мало двигаюсь", "без активности"]):
        return ActivityLevel.sedentary

    # Default minimal
    return ActivityLevel.sedentary


def _decide_rate_and_target_cal(
    tdee: float,
    goal: Goal,
    speed: Speed | None,
    weight_kg: float,
) -> tuple[float, float]:
    """
    Returns (weekly_rate_kg, target_calories).

    - Convert speed preset to kg/week using percent of body weight.
    - Apply safety limits on rate and daily calorie delta.
    - Maintain -> rate=0, target=tdee.
    """
    if goal == Goal.maintain or speed is None:
        return 0.0, float(tdee)

    # requested weekly rate in kg/week (absolute value)
    requested_rate = SPEED_PERCENT_BY_WEIGHT.get(speed, 0.003) * weight_kg

    if goal == Goal.lose:
        # cap by max percent/week
        rate = min(requested_rate, MAX_LOSS_RATE * weight_kg)
        # translate to kcal/day deficit
        deficit = rate * 7700.0 / 7.0
        # cap by TDEE-based safety
        max_deficit = min(MAX_DEFICIT_ABS, MAX_DEFICIT_FRAC * tdee)
        if deficit > max_deficit:
            deficit = max_deficit
            rate = deficit * 7.0 / 7700.0
        target = max(100.0, tdee - deficit)
        return rate, target

    if goal == Goal.gain:
        rate = min(requested_rate, MAX_GAIN_RATE * weight_kg)
        surplus = rate * 7700.0 / 7.0
        # keep surplus within 200..500 kcal/day
        if surplus < GAIN_MIN_SURPLUS:
            surplus = GAIN_MIN_SURPLUS
            rate = surplus * 7.0 / 7700.0
        if surplus > GAIN_MAX_SURPLUS:
            surplus = GAIN_MAX_SURPLUS
            rate = surplus * 7.0 / 7700.0
        target = tdee + surplus
        return rate, target

    return 0.0, float(tdee)


def _macro_split(goal: Goal) -> tuple[float, float, float]:
    """Return protein, fat, carb fractions for calories (sum to 1.0)."""
    if goal == Goal.lose:
        return 0.35, 0.25, 0.40
    if goal == Goal.gain:
        return 0.30, 0.30, 0.40
    return 0.30, 0.30, 0.40  # maintain


def _cal_to_grams(calories: float, p_frac: float, f_frac: float, c_frac: float) -> tuple[int, int, int]:
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
    - Convert speed preset to weekly rate (kg/нед) with safety limits; compute target calories
    - Split macros per TZ and convert to grams
    """
    # Activity
    activity_level = data.activity_level or _infer_activity_level(data.activity_text)
    activity_multiplier = ACTIVITY_MULTIPLIERS[activity_level]

    # BMR and TDEE
    bmr = calc_bmr_mifflin(data.gender, data.age, data.weight_kg, data.height_cm)
    tdee = bmr * activity_multiplier

    # Rate and target calories according to TZ presets and safety
    weekly_rate_kg, target_cal = _decide_rate_and_target_cal(
        tdee=tdee,
        goal=data.goal,
        speed=data.speed,
        weight_kg=data.weight_kg,
    )

    # Macros
    p_frac, f_frac, c_frac = _macro_split(data.goal)
    p_g, f_g, c_g = _cal_to_grams(target_cal, p_frac, f_frac, c_frac)

    # ETA
    eta: date | None = None
    if data.goal != Goal.maintain and data.goal_weight_kg is not None and weekly_rate_kg > 0:
        delta = abs(data.weight_kg - data.goal_weight_kg)
        if delta > 0:
            weeks = delta / weekly_rate_kg
            eta = date.today() + timedelta(days=round(weeks * 7))

    result = DailyPlan(
        calories=round(target_cal),
        protein_g=p_g,
        fat_g=f_g,
        carbs_g=c_g,
        sources=SOURCES,
        tdee=round(tdee),
        weekly_rate_kg=round(weekly_rate_kg, 2),
        eta_date=eta,
    )

    logger.info(
        "daily_plan computed | user_id={}, gender={}, age={}, w={}, h={}, act={}, bmr={}, tdee={}, target={}, rate_kg_per_wk={}, eta={}, p/f/c={}/{}/{}",
        data.user_id,
        data.gender,
        data.age,
        data.weight_kg,
        data.height_cm,
        activity_level.value,
        round(bmr, 2),
        round(tdee, 2),
        result.calories,
        weekly_rate_kg,
        eta,
        result.protein_g,
        result.fat_g,
        result.carbs_g,
    )

    return result
