from __future__ import annotations

from typing import Any, Dict, List, Tuple
from datetime import datetime, timedelta, timezone, date as date_cls, time as dtime

from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import MealModel, OnboardingAnswerModel
from bot.services.users import get_user_tzinfo
from bot.core.loader import redis_client
from bot.core.config import settings
from bot.services.foodai import _openai_request  # reuse Responses helper

WEEKDAY_RU_SHORT = [
    "Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"
]


async def aggregate_last7_days(user_id: int) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """Return per-day aggregates for last 7 calendar days (D0..D-6) in user's TZ.

    Returns (days, totals) where days is a list of dicts with keys:
      - date: datetime.date (local date)
      - start_utc, end_utc: UTC window
      - total_cal, total_p, total_f, total_c: floats
      - has_entries: bool
    And totals is week-level sums across all 7 days.
    """
    days: List[Dict[str, Any]] = []
    week_totals = {"cal": 0.0, "p": 0.0, "f": 0.0, "c": 0.0}

    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        now_local = datetime.now(tz)
        today_local = now_local.date()

        for i in range(0, 7):
            d_local = today_local - timedelta(days=i)
            start_local = datetime.combine(d_local, dtime(0, 0), tz)
            end_local = start_local + timedelta(days=1)
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = end_local.astimezone(timezone.utc)

            res = await session.execute(
                select(MealModel)
                .where(
                    (MealModel.user_id == user_id)
                    & (MealModel.status == "saved")
                    & (MealModel.consumed_at >= start_utc)
                    & (MealModel.consumed_at < end_utc)
                )
                .order_by(MealModel.consumed_at.asc())
            )
            meals = list(res.scalars().all())

            total_cal = float(sum(int(m.calories or 0) for m in meals))
            total_p = float(sum(float(m.protein_g or 0.0) for m in meals))
            total_f = float(sum(float(m.fat_g or 0.0) for m in meals))
            total_c = float(sum(float(m.carbs_g or 0.0) for m in meals))

            week_totals["cal"] += total_cal
            week_totals["p"] += total_p
            week_totals["f"] += total_f
            week_totals["c"] += total_c

            days.append({
                "date": d_local,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "total_cal": total_cal,
                "total_p": total_p,
                "total_f": total_f,
                "total_c": total_c,
                "has_entries": len(meals) > 0,
            })

    return days, week_totals


def _advice_cache_key(user_id: int, window_start: date_cls) -> str:
    return f"history:advice:v1:{user_id}:{window_start.isoformat()}"


async def get_week_advice(user_id: int, days: List[Dict[str, Any]]) -> str | None:
    """Return short weekly advice using LLM with 5h Redis cache. Returns None on failure."""
    if not days:
        return None
    # Cache key: start of the 7-day window (oldest date)
    window_start = days[-1]["date"]
    key = _advice_cache_key(user_id, window_start)

    try:
        cached = await redis_client.get(key)
        if cached:
            try:
                return cached.decode("utf-8")
            except Exception:
                return str(cached)
    except Exception:
        pass

    # Build minimal prompt from aggregates (+ user's plan/weight when available)
    try:
        total_days_with = sum(1 for d in days if d.get("has_entries"))
        total_cal = sum(float(d.get("total_cal") or 0.0) for d in days)
        total_p = sum(float(d.get("total_p") or 0.0) for d in days)
        avg_cal = (total_cal / total_days_with) if total_days_with else 0.0
        avg_p = (total_p / total_days_with) if total_days_with else 0.0

        # Load user's plan and weight (if available)
        plan_cal: float | None = None
        plan_p: float | None = None
        weight_kg: float | None = None

        try:
            async with sessionmaker() as session:
                oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
                if oa and isinstance(getattr(oa, "daily_plan", None), dict):
                    plan = oa.daily_plan or {}
                    try:
                        plan_cal = float(plan.get("calories") or 0)
                    except Exception:
                        plan_cal = None
                    try:
                        plan_p = float(plan.get("protein_g") or 0)
                    except Exception:
                        plan_p = None
                # weight may be stored in raw onboarding data
                try:
                    data = (oa.data if oa and isinstance(getattr(oa, "data", None), dict) else {}) or {}
                    w = data.get("weight_kg")
                    weight_kg = float(w) if w is not None else None
                except Exception:
                    weight_kg = None
        except Exception:
            plan_cal = plan_p = weight_kg = None

        # Derived protein per kg if weight known
        avg_p_per_kg: float | None = (avg_p / weight_kg) if (weight_kg and weight_kg > 0) else None
        target_p_per_kg: float | None = ((plan_p or 0) / weight_kg) if (weight_kg and weight_kg > 0 and plan_p) else None

        instructions = (
            "Ты — ИИ‑нутрициолог. Дай очень короткий практичный совет (1–2 предложения) на русском, без markdown. "
            "Учитывай только калории и белок, сравнивай средние с целями плана, если они есть. "
            "Если известен вес, можно упомянуть белок в г/кг. Не придумывай фактов, не упоминай уверенность. "
            "Строго не более 220 символов."
        )

        parts: list[str] = []
        parts.append(f"Средние калории: {int(avg_cal)} ккал. Средний белок: {avg_p:.1f} г.")
        parts.append(f"Дней с записями: {total_days_with} из 7.")
        if plan_cal and plan_cal > 0:
            parts.append(f"Цель калорий: {int(plan_cal)} ккал/день.")
        if plan_p and plan_p > 0:
            parts.append(f"Цель белка: {int(plan_p)} г/день.")
        if weight_kg and weight_kg > 0:
            parts.append(f"Вес: {weight_kg:g} кг.")
            if avg_p_per_kg is not None:
                parts.append(f"Средний белок на кг: {avg_p_per_kg:.1f} г/кг.")
            if target_p_per_kg is not None:
                parts.append(f"Цель белка на кг: {target_p_per_kg:.1f} г/кг.")
        parts.append("Дай практичный совет на неделю, без общих фраз.")
        user_text = " ".join(parts)
        payload = {
            "model": getattr(settings, "RECOMMENDER_MODEL", "gpt-5-mini"),
            "instructions": instructions,
            "reasoning": {"effort": getattr(settings, "FOODAI_REASONING_EFFORT", "minimal")},
            "text": {"verbosity": getattr(settings, "FOODAI_TEXT_VERBOSITY", "low")},
            "max_output_tokens": 200,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
        }
        raw = await _openai_request("responses", payload)
        advice = None
        if isinstance(raw, str):
            advice = raw.strip().replace("\n", " ")
        if advice:
            try:
                await redis_client.setex(key, int(5 * 3600), advice)
            except Exception:
                pass
            return advice
    except Exception:
        return None
    return None


def weekday_ru(d: date_cls) -> str:
    # Python weekday: Monday=0..Sunday=6
    return WEEKDAY_RU_SHORT[d.weekday()]


# --- Add-in-day (backdating) helpers ---
async def set_add_in_day_target(user_id: int, local_date_iso: str, ttl_sec: int = 900) -> None:
    try:
        await redis_client.setex(f"history:add_target:{user_id}", int(ttl_sec), local_date_iso)
    except Exception:
        pass


async def get_add_in_day_target(user_id: int) -> str | None:
    try:
        v = await redis_client.get(f"history:add_target:{user_id}")
        if not v:
            return None
        try:
            return v.decode("utf-8")
        except Exception:
            return str(v)
    except Exception:
        return None


async def clear_add_in_day_target(user_id: int) -> None:
    try:
        await redis_client.delete(f"history:add_target:{user_id}")
    except Exception:
        pass
