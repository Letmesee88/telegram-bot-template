from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, time as dtime, date
from typing import Optional, Tuple, List

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.database.database import sessionmaker
from bot.core.loader import redis_client
from bot.database.models import WeightLogModel, OnboardingAnswerModel
from bot.services.users import get_user_tzinfo


@dataclass
class WeightSummary:
    current: Optional[float]
    goal: Optional[float]
    left: Optional[float]
    progress_pct: Optional[float]


async def _now_local_and_date(user_id: int) -> Tuple[datetime, date]:
    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        now_local = datetime.now(tz)
        return now_local, now_local.date()


def parse_weight_input(s: str) -> Optional[float]:
    if not isinstance(s, str):
        return None
    s = s.strip().replace(",", ".")
    try:
        v = float(s)
    except Exception:
        return None
    if not (30.0 <= v <= 300.0):
        return None
    return round(v, 1)


async def save_weight(user_id: int, value_kg: float) -> Tuple[float, date]:
    now_local, local_date = await _now_local_and_date(user_id)
    recorded_at_utc = now_local.astimezone(timezone.utc)
    async with sessionmaker() as session:
        # Upsert by (user_id, recorded_local_date)
        existing = await session.scalar(
            select(WeightLogModel).where(
                (WeightLogModel.user_id == user_id)
                & (WeightLogModel.recorded_local_date == local_date)
            )
        )
        if existing:
            existing.weight_kg = float(value_kg)
            existing.recorded_at = recorded_at_utc
        else:
            wl = WeightLogModel(
                user_id=user_id,
                weight_kg=float(value_kg),
                recorded_at=recorded_at_utc,
                recorded_local_date=local_date,
                source="manual",
            )
            session.add(wl)
        await session.commit()
    # Invalidate account summary cache so UI reflects the change immediately
    try:
        await redis_client.delete(f"account:summary:{user_id}")
    except Exception:
        pass
    return float(value_kg), local_date


async def get_current_weight(user_id: int) -> Optional[float]:
    async with sessionmaker() as session:
        wl = await session.scalar(
            select(WeightLogModel)
            .where(WeightLogModel.user_id == user_id)
            .order_by(WeightLogModel.recorded_at.desc())
            .limit(1)
        )
        if wl and wl.weight_kg is not None:
            return float(wl.weight_kg)
        # fallback to onboarding
        oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        data = (oa.data if oa and isinstance(getattr(oa, "data", None), dict) else {}) or {}
        if data.get("weight_kg") is not None:
            try:
                return float(data.get("weight_kg"))
            except Exception:
                return None
        return None


async def get_goal_weight(user_id: int) -> Optional[float]:
    async with sessionmaker() as session:
        oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        data = (oa.data if oa and isinstance(getattr(oa, "data", None), dict) else {}) or {}
        if data.get("goal_weight_kg") is not None:
            try:
                return float(data.get("goal_weight_kg"))
            except Exception:
                return None
        return None


async def get_start_weight(user_id: int) -> Optional[float]:
    async with sessionmaker() as session:
        oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        data = (oa.data if oa and isinstance(getattr(oa, "data", None), dict) else {}) or {}
        if data.get("start_weight_kg") is not None:
            try:
                return float(data.get("start_weight_kg"))
            except Exception:
                pass
        # else earliest from logs
        wl = await session.scalar(
            select(WeightLogModel)
            .where(WeightLogModel.user_id == user_id)
            .order_by(WeightLogModel.recorded_at.asc())
            .limit(1)
        )
        if wl and wl.weight_kg is not None:
            return float(wl.weight_kg)
        # fallback to onboarding current
        if data.get("weight_kg") is not None:
            try:
                return float(data.get("weight_kg"))
            except Exception:
                return None
        return None


def compute_progress(start_w: Optional[float], current_w: Optional[float], goal_w: Optional[float]) -> Optional[float]:
    if start_w is None or current_w is None or goal_w is None or goal_w == start_w:
        return None
    if goal_w < start_w:
        # losing weight progress
        numerator = max(0.0, start_w - current_w)
        denom = max(0.0, start_w - goal_w)
    else:
        # gaining weight progress
        numerator = max(0.0, current_w - start_w)
        denom = max(0.0, goal_w - start_w)
    if denom <= 0:
        return 0.0
    return max(0.0, min(100.0, (numerator / denom) * 100.0))


async def build_my_weight_text(user_id: int) -> str:
    current = await get_current_weight(user_id)
    goal = await get_goal_weight(user_id)
    start = await get_start_weight(user_id)
    left = (abs(current - goal) if (current is not None and goal is not None) else None)
    progress = compute_progress(start, current, goal)

    lines: list[str] = ["⚖️ Мой вес", ""]
    lines.append(f"• Текущий вес: {current:.1f} кг" if current is not None else "• Текущий вес: Нет данных")
    lines.append(f"• Целевой вес: {goal:.1f} кг" if goal is not None else "• Целевой вес: Нет данных")
    if left is not None:
        lines.append(f"• До цели: {left:.1f} кг")
    else:
        lines.append("• До цели: Нет данных")
    return "\n".join(lines)


@dataclass
class WeightHistoryPage:
    text: str
    page: int
    has_prev: bool
    has_next: bool


async def build_history_page(user_id: int, page: int, page_size: int = 20) -> WeightHistoryPage:
    page = max(1, page)
    offset = (page - 1) * page_size
    async with sessionmaker() as session:
        res = await session.execute(
            select(WeightLogModel)
            .where(WeightLogModel.user_id == user_id)
            .order_by(WeightLogModel.recorded_at.desc())
            .offset(offset)
            .limit(page_size + 1)
        )
        rows: List[WeightLogModel] = list(res.scalars().all())
    has_next = len(rows) > page_size
    rows = rows[:page_size]
    has_prev = page > 1

    lines: list[str] = ["📉 История моего веса"]
    if not rows:
        lines.append("Нет записей")
    else:
        for wl in rows:
            d = wl.recorded_local_date
            try:
                d_str = d.strftime("%d.%m.%Y")
            except Exception:
                d_str = str(d)
            lines.append(f"• {d_str}: {float(wl.weight_kg):.1f} кг")
    return WeightHistoryPage(text="\n".join(lines), page=page, has_prev=has_prev, has_next=has_next)
