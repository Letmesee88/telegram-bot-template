from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from bot.database.models import (
    DailyIntakeModel,
    MealItemModel,
    MealModel,
    UserModel,
)
from bot.services.foodai import analyze_text


@pytest.mark.asyncio
async def test_foodai_smoke_create_meal_and_upsert_daily_intake(apply_migrations, db_session, ensure_user) -> None:
    # 1) Ensure user and enable FoodAI
    user_id = await ensure_user(user_id=424242, first_name="FoodAI")
    user = await db_session.get(UserModel, user_id)
    user.foodai_enabled_at = datetime.now(timezone.utc)
    await db_session.commit()

    # 2) Create a draft meal (text source)
    meal = MealModel(
        user_id=user_id,
        source="text",
        status="draft",
        title="Овсянка с бананом",
    )
    db_session.add(meal)
    await db_session.flush()
    meal_id = meal.id
    await db_session.commit()

    # Analyze text (stub)
    result = await analyze_text("овсяная каша с бананом и молоком")

    # 3) Persist analysis to meal + items, upsert daily intake
    today_utc = datetime.now(timezone.utc).date()

    meal = await db_session.get(MealModel, meal_id)
    assert meal is not None

    calories = int(result.get("calories") or 0)
    protein_g = float(result.get("protein_g") or 0)
    fat_g = float(result.get("fat_g") or 0)
    carbs_g = float(result.get("carbs_g") or 0)
    items = list(result.get("items") or [])
    references = result.get("references") or {"source": "stub"}

    meal.calories = calories
    meal.protein_g = protein_g
    meal.fat_g = fat_g
    meal.carbs_g = carbs_g
    meal.weight_g = float(result.get("weight_g") or 0)
    meal.confidence = float(result.get("confidence") or 0)
    meal.analysis_json = result
    meal.references = references
    meal.status = "saved"

    for it in items:
        db_session.add(
            MealItemModel(
                meal_id=meal.id,
                name=str(it.get("name") or "Блюдо"),
                weight_g=float(it.get("weight_g") or 0) if it.get("weight_g") is not None else None,
                calories=float(it.get("calories") or 0) if it.get("calories") is not None else None,
                protein_g=float(it.get("protein_g") or 0) if it.get("protein_g") is not None else None,
                fat_g=float(it.get("fat_g") or 0) if it.get("fat_g") is not None else None,
                carbs_g=float(it.get("carbs_g") or 0) if it.get("carbs_g") is not None else None,
            )
        )

    di = await db_session.scalar(
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
        db_session.add(di)

    # accumulate
    di.calories = int(int(di.calories or 0) + calories)
    di.protein_g = float(float(di.protein_g or 0) + protein_g)
    di.fat_g = float(float(di.fat_g or 0) + fat_g)
    di.carbs_g = float(float(di.carbs_g or 0) + carbs_g)

    await db_session.commit()

    # 4) Verify persisted state
    saved_meal = await db_session.get(MealModel, meal_id)
    assert saved_meal is not None
    assert saved_meal.status == "saved"
    assert int(saved_meal.calories or 0) == calories

    items_cur = await db_session.scalars(select(MealItemModel).where(MealItemModel.meal_id == meal_id))
    items_list = list(items_cur)
    assert len(items_list) == len(items)

    di_after = await db_session.scalar(
        select(DailyIntakeModel).where(
            (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc == today_utc)
        )
    )
    assert di_after is not None
    assert int(di_after.calories) >= calories

    # 5) Add second meal to verify upsert accumulation
    meal2 = MealModel(
        user_id=user_id,
        source="text",
        status="draft",
        title="Йогурт",
    )
    db_session.add(meal2)
    await db_session.flush()

    result2 = await analyze_text("йогурт греческий 200г")
    calories2 = int(result2.get("calories") or 0)

    meal2.calories = calories2
    meal2.status = "saved"

    di2 = await db_session.scalar(
        select(DailyIntakeModel).where(
            (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc == today_utc)
        )
    )
    if di2 is None:
        di2 = DailyIntakeModel(
            user_id=user_id,
            date_utc=today_utc,
            calories=0,
            protein_g=0,
            fat_g=0,
            carbs_g=0,
        )
        db_session.add(di2)

    prev = int(di2.calories or 0)
    di2.calories = int(prev + calories2)

    await db_session.commit()
    # Ensure ORM state reflects DB
    await db_session.refresh(di2)
    assert int(di2.calories) == prev + calories2

    di_final = await db_session.scalar(
        select(DailyIntakeModel).where(
            (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc == today_utc)
        )
    )
    assert di_final is not None
    # Compare against snapshot 'prev' taken just before update to avoid relying on earlier instance state
    assert int(di_final.calories) == prev + calories2
