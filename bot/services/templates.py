from __future__ import annotations
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, literal, select

from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.database.models import (
    DailyIntakeModel,
    MealItemModel,
    MealModel,
    MealTemplateItemModel,
    MealTemplateModel,
)
from bot.services.analytics import analytics

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

MAX_TEMPLATES_PER_CATEGORY = 20
CATEGORY_VALUES = {"breakfast", "lunch", "dinner", "snack"}


def _norm_title(title: str) -> str:
    return (title or "").strip()


async def _count_in_category(session: AsyncSession, user_id: int, category: str) -> int:
    res = await session.execute(
        select(func.count()).select_from(MealTemplateModel).where(
            (MealTemplateModel.user_id == user_id) & (MealTemplateModel.category == category)
        )
    )
    return int(res.scalar_one() or 0)


async def _exists_duplicate(session: AsyncSession, user_id: int, category: str, title: str) -> bool:
    # Match DB functional unique index lower(btrim(title))
    title_norm = _norm_title(title)
    res = await session.execute(
        select(literal(1)).select_from(MealTemplateModel).where(
            and_(
                MealTemplateModel.user_id == user_id,
                MealTemplateModel.category == category,
                func.lower(func.btrim(MealTemplateModel.title)) == func.lower(func.btrim(literal(title_norm))),
            )
        ).limit(1)
    )
    return res.scalar_one_or_none() is not None


async def create_template_from_meal(
    session: AsyncSession,
    user_id: int,
    meal_id: int,
    category: str,
) -> MealTemplateModel:
    if category not in CATEGORY_VALUES:
        msg = "invalid_category"
        raise ValueError(msg)

    # Counters and validations
    cnt = await _count_in_category(session, user_id, category)
    if cnt >= MAX_TEMPLATES_PER_CATEGORY:
        msg = "limit_reached"
        raise ValueError(msg)

    meal = await session.get(MealModel, meal_id)
    if not meal or meal.user_id != user_id:
        msg = "meal_not_found"
        raise ValueError(msg)

    base_title = _norm_title(meal.title or "") or "Блюдо"
    if len(base_title) > 255:
        base_title = base_title[:255]
    # Ensure uniqueness by appending (n) if needed
    title = base_title
    if await _exists_duplicate(session, user_id, category, title):
        for i in range(2, 100):
            candidate = f"{base_title} ({i})"
            if len(candidate) > 255:
                candidate = candidate[:255]
            if not await _exists_duplicate(session, user_id, category, candidate):
                title = candidate
                break
        else:
            # Too many duplicates, fallback to timestamp-ish variant
            from datetime import datetime
            suffix = datetime.utcnow().strftime("%H%M%S")
            candidate = f"{base_title} {suffix}"
            title = candidate[:255]

    # Build template aggregates from meal; fallback to items sum if needed
    cal = int(meal.calories or 0)
    p = float(meal.protein_g or 0)
    f = float(meal.fat_g or 0)
    c = float(meal.carbs_g or 0)
    w = float(meal.weight_g or 0)

    tpl = MealTemplateModel(
        user_id=user_id,
        category=category,
        title=title,
        calories=cal or None,
        protein_g=p or None,
        fat_g=f or None,
        carbs_g=c or None,
        weight_g=w or None,
    )
    session.add(tpl)
    await session.flush()  # get tpl.id

    # Copy items from meal
    items: Sequence[MealItemModel] = list(meal.items or [])
    if len(items) > 50:
        items = items[:50]
    for it in items:
        session.add(
            MealTemplateItemModel(
                template_id=tpl.id,
                name=(it.name or "Ингредиент")[:255],
                weight_g=float(it.weight_g) if it.weight_g is not None else None,
                calories=float(it.calories) if it.calories is not None else None,
                protein_g=float(it.protein_g) if it.protein_g is not None else None,
                fat_g=float(it.fat_g) if it.fat_g is not None else None,
                carbs_g=float(it.carbs_g) if it.carbs_g is not None else None,
            )
        )

    await session.commit()

    # Analytics
    if analytics.logger:
        analytics.fire_event(
            BaseEvent(
                user_id=user_id,
                event_type="Rec:OtherClicked",
                event_properties=EventProperties(text=f"Templates:CreateSucceeded; template_id={tpl.id}, category={category}"),
                plan=Plan(branch="Templates", source="Bot", version="v1"),
            )
        )

    return tpl


async def list_categories_with_counts(session: AsyncSession, user_id: int) -> dict[str, int]:
    res = await session.execute(
        select(MealTemplateModel.category, func.count()).where(MealTemplateModel.user_id == user_id).group_by(MealTemplateModel.category)
    )
    rows = res.all()
    base = dict.fromkeys(CATEGORY_VALUES, 0)
    for cat, cnt in rows:
        base[str(cat)] = int(cnt or 0)
    return base


async def list_templates(session: AsyncSession, user_id: int, category: str) -> list[MealTemplateModel]:
    res = await session.execute(
        select(MealTemplateModel)
        .where((MealTemplateModel.user_id == user_id) & (MealTemplateModel.category == category))
        .order_by(MealTemplateModel.created_at.desc())
    )
    return list(res.scalars().all())


async def delete_template(session: AsyncSession, user_id: int, template_id: int) -> None:
    tpl = await session.get(MealTemplateModel, template_id)
    if not tpl or tpl.user_id != user_id:
        msg = "not_found"
        raise ValueError(msg)
    await session.delete(tpl)
    await session.commit()

    if analytics.logger:
        analytics.fire_event(
            BaseEvent(
                user_id=user_id,
                event_type="Rec:OtherClicked",
                event_properties=EventProperties(text=f"Templates:Deleted; template_id={template_id}, category={tpl.category}"),
                plan=Plan(branch="Templates", source="Bot", version="v1"),
            )
        )


async def create_meal_draft_from_template(session: AsyncSession, user_id: int, template_id: int) -> int:
    tpl = await session.get(MealTemplateModel, template_id)
    if not tpl or tpl.user_id != user_id:
        msg = "not_found"
        raise ValueError(msg)

    meal = MealModel(
        user_id=user_id,
        source="template",
        status="draft",
        title=tpl.title,
        calories=int(tpl.calories or 0) or None,
        protein_g=float(tpl.protein_g or 0) or None,
        fat_g=float(tpl.fat_g or 0) or None,
        carbs_g=float(tpl.carbs_g or 0) or None,
        weight_g=float(tpl.weight_g or 0) or None,
    )
    session.add(meal)
    await session.flush()

    res = await session.execute(
        select(MealTemplateItemModel).where(MealTemplateItemModel.template_id == template_id)
    )
    for it in res.scalars().all():
        session.add(
            MealItemModel(
                meal_id=meal.id,
                name=(it.name or "Ингредиент")[:255],
                weight_g=float(it.weight_g) if it.weight_g is not None else None,
                calories=float(it.calories) if it.calories is not None else None,
                protein_g=float(it.protein_g) if it.protein_g is not None else None,
                fat_g=float(it.fat_g) if it.fat_g is not None else None,
                carbs_g=float(it.carbs_g) if it.carbs_g is not None else None,
            )
        )
    await session.commit()

    if analytics.logger:
        analytics.fire_event(
            BaseEvent(
                user_id=user_id,
                event_type="Rec:OtherClicked",
                event_properties=EventProperties(text=f"Templates:DraftCreated; meal_id={meal.id}, template_id={template_id}"),
                plan=Plan(branch="Templates", source="Bot", version="v1"),
            )
        )

    return int(meal.id)


async def apply_template_immediately(session: AsyncSession, user_id: int, template_id: int) -> int:
    """Create a saved meal immediately and update DailyIntake for UTC date-now.

    Note: Normal UX показывает карточку черновика и сохраняет по кнопке. Этот метод —
    опциональный быстрый путь без карточки.
    """
    meal_id = await create_meal_draft_from_template(session, user_id, template_id)

    # Update daily intake like cb_foodai_save does
    today_utc = datetime.now(timezone.utc).date()
    di = await session.scalar(
        select(DailyIntakeModel).where(
            (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc == today_utc)
        )
    )
    if di is None:
        di = DailyIntakeModel(
            user_id=user_id,
            date_utc=today_utc,
            calories=0,
            protein_g=0,
            fat_g=0,
            carbs_g=0,
        )
        session.add(di)

    meal = await session.get(MealModel, meal_id)
    if not meal or meal.user_id != user_id:
        msg = "not_found"
        raise ValueError(msg)

    di.calories = int(int(di.calories or 0) + int(meal.calories or 0))
    di.protein_g = float(float(di.protein_g or 0) + float(meal.protein_g or 0))
    di.fat_g = float(float(di.fat_g or 0) + float(meal.fat_g or 0))
    di.carbs_g = float(float(di.carbs_g or 0) + float(meal.carbs_g or 0))

    meal.status = "saved"
    await session.commit()

    if analytics.logger:
        analytics.fire_event(
            BaseEvent(
                user_id=user_id,
                event_type="Rec:OtherClicked",
                event_properties=EventProperties(text=f"Templates:Applied; meal_id={meal_id}, template_id={template_id}"),
                plan=Plan(branch="Templates", source="Bot", version="v1"),
            )
        )

    return int(meal_id)
