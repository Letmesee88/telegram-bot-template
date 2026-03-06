from __future__ import annotations
from datetime import date
from enum import Enum
from pydantic import BaseModel, Field, field_validator


class Gender(str, Enum):
    male = "male"
    female = "female"


class Goal(str, Enum):
    lose = "lose"          # похудение
    gain = "gain"          # набор
    maintain = "maintain"  # поддержать


class Speed(str, Enum):
    comfort = "COMFORT"
    effort = "EFFORT"
    fast = "FAST"


class ActivityLevel(str, Enum):
    sedentary = "sedentary"
    light = "light"
    moderate = "moderate"
    active = "active"
    athlete = "athlete"


ACTIVITY_MULTIPLIERS: dict[ActivityLevel, float] = {
    ActivityLevel.sedentary: 1.2,
    ActivityLevel.light: 1.375,
    ActivityLevel.moderate: 1.55,
    ActivityLevel.active: 1.725,
    ActivityLevel.athlete: 1.9,
}


class OnboardingData(BaseModel):
    user_id: int = Field(..., description="Telegram user id")
    gender: Gender
    age: int
    weight_kg: float
    height_cm: float
    activity_text: str | None = None
    activity_level: ActivityLevel | None = None
    goal: Goal
    goal_weight_kg: float | None = None
    speed: Speed | None = None

    @field_validator("age")
    @classmethod
    def validate_age(cls, v: int) -> int:
        if not (1 <= v <= 120):
            msg = "age must be between 1 and 120"
            raise ValueError(msg)
        return v

    @field_validator("weight_kg")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        if not (30 <= v <= 300):
            msg = "weight must be between 30 and 300 kg"
            raise ValueError(msg)
        return v

    @field_validator("height_cm")
    @classmethod
    def validate_height(cls, v: float) -> float:
        if not (120 <= v <= 250):
            msg = "height must be between 120 and 250 cm"
            raise ValueError(msg)
        return v

    # activity_text is optional in the new button-based flow


class DailyPlan(BaseModel):
    calories: int
    protein_g: int
    fat_g: int
    carbs_g: int
    sources: list[str]
    tdee: int
    weekly_rate_kg: float
    eta_date: date | None
