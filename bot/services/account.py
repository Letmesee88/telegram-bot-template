from __future__ import annotations

from datetime import datetime, timedelta, timezone, time as dtime
from typing import Optional

from sqlalchemy import select

from bot.core.loader import redis_client
from bot.database.database import sessionmaker
from bot.database.models import MealModel, OnboardingAnswerModel, UserModel
from bot.services.users import get_user_tzinfo
from bot.core.config import settings
from bot.services.weight import get_current_weight, get_start_weight, get_goal_weight, compute_progress


async def _aggregate_last30_days_cal(user_id: int) -> tuple[int, int]:
    total_cal = 0
    days_with = 0
    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        now_local = datetime.now(tz)
        today_local = now_local.date()
        for i in range(0, 30):
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
            )
            meals = list(res.scalars().all())
            day_cal = sum(int(m.calories or 0) for m in meals)
            total_cal += int(day_cal)
            if day_cal > 0:
                days_with += 1
    return total_cal, days_with


def _format_signed_int(v: int) -> str:
    return f"{v:+d}"


def _format_signed_pct(v: float) -> str:
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"{sign}{abs(v):.1f}%"


async def _load_onboarding(user_id: int) -> tuple[Optional[dict], Optional[dict]]:
    async with sessionmaker() as session:
        oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        data = (oa.data if oa and isinstance(getattr(oa, "data", None), dict) else {}) or {}
        plan = (oa.daily_plan if oa and isinstance(getattr(oa, "daily_plan", None), dict) else {}) or {}
        return data, plan


async def _load_user(user_id: int) -> Optional[UserModel]:
    async with sessionmaker() as session:
        return await session.scalar(select(UserModel).where(UserModel.id == user_id))


async def get_account_summary_text(user_id: int) -> str:
    key = f"account:summary:{user_id}"
    try:
        cached = await redis_client.get(key)
        if cached:
            try:
                return cached.decode("utf-8")
            except Exception:
                return str(cached)
    except Exception:
        pass

    total_cal, days_with = await _aggregate_last30_days_cal(user_id)
    avg_cal = int(round(total_cal / days_with)) if days_with > 0 else 0

    data, plan = await _load_onboarding(user_id)
    plan_cal = int(plan.get("calories") or 0)

    if plan_cal > 0:
        delta_cal = avg_cal - plan_cal
        delta_pct = (float(delta_cal) / float(plan_cal)) * 100.0
        deviation = f"{_format_signed_int(delta_cal)} ккал ({_format_signed_pct(delta_pct)})"
        norm_text = f"{plan_cal} ккал"
    else:
        deviation = "Нет данных"
        norm_text = "Нет данных"

    # Resolve weights using weight service (logs-aware with onboarding fallbacks)
    start_w = await get_start_weight(user_id)
    current_w = await get_current_weight(user_id)
    goal_w = await get_goal_weight(user_id)

    weight_block_lines: list[str] = []
    if current_w is not None and goal_w is not None and start_w is not None and (goal_w != start_w):
        progress = compute_progress(start_w, current_w, goal_w) or 0.0
        left = max(0.0, (goal_w - current_w)) if goal_w >= current_w else max(0.0, (current_w - goal_w))
        left_disp = f"{left:.1f} кг"
        weight_block_lines.append(f"Текущий: {current_w:.1f} кг → Цель: {goal_w:.1f} кг")
        weight_block_lines.append(f"Осталось: {left_disp}")
    else:
        cw = f"{current_w:.1f} кг" if current_w is not None else "Нет данных"
        gw = f"{goal_w:.1f} кг" if goal_w is not None else "Нет данных"
        weight_block_lines.append(f"Текущий: {cw} → Цель: {gw}")
        weight_block_lines.append("Осталось: Нет данных")

    user = await _load_user(user_id)
    days_with_us_text = "Нет данных"
    if user and getattr(user, "created_at", None):
        try:
            tz = await get_user_tzinfo(None, user_id)  # function ignores session when None
        except Exception:
            tz = timezone.utc
        days_with_us = max(0, (datetime.now(tz).date() - user.created_at.date()).days)
        if days_with_us < 25:
            days_with_us_text = "С нами недавно — добро пожаловать!"
        elif days_with_us < 50:
            days_with_us_text = "25 дней вместе — супер старт!"
        elif days_with_us < 75:
            days_with_us_text = "50 дней — уверенная стабильность"
        elif days_with_us < 100:
            days_with_us_text = "75 дней — отличная дисциплина"
        elif days_with_us < 125:
            days_with_us_text = "100 дней — постоянство впечатляет"
        elif days_with_us < 150:
            days_with_us_text = "125 дней — высокий уровень привычки"
        elif days_with_us < 175:
            days_with_us_text = "150+ дней — сильная мотивация"
        elif days_with_us < 200:
            days_with_us_text = "175+ дней — пример для других"
        else:
            days_with_us_text = f"{days_with_us} дней — выдающаяся серия"

    # progress achievement
    progress_text = "Начало пути — первый шаг сделан 💪"
    try:
        if current_w is not None and goal_w is not None and start_w is not None and (goal_w != start_w):
            progress_val = compute_progress(start_w, current_w, goal_w) or 0.0
            if progress_val < 25:
                progress_text = "Начало пути — первый шаг сделан 💪"
            elif progress_val < 50:
                progress_text = "Хороший темп — уже четверть пути ✅"
            elif progress_val < 75:
                progress_text = "Половина позади — держим курс 🚀"
            elif progress_val < 100:
                progress_text = "Близко к цели — финишная прямая ✨"
            else:
                progress_text = "Цель достигнута! Отличная работа 🏁"
    except Exception:
        progress_text = "Начало пути — первый шаг сделан 💪"

    lines: list[str] = []
    lines.append("👋 Добро пожаловать в личный кабинет!")
    lines.append("")
    lines.append("🍽 Твое питание за последние 30 дней")
    lines.append(f"Среднесуточно: {avg_cal} ккал | Норма: {norm_text}")
    lines.append(f"Отклонение: {deviation}")
    lines.append("")
    lines += ["⚖️ Контроль веса"] + weight_block_lines
    lines.append("")
    lines.append("🌟 Твои достижения")
    lines.append(f"✅ {progress_text}")
    lines.append(f"✅ {days_with_us_text}")

    text = "\n".join(lines)
    try:
        await redis_client.setex(key, 60, text)
    except Exception:
        pass
    return text
