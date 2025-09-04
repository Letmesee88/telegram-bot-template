from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router, types
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import DailyIntakeModel, MealItemModel, MealModel, MealPhotoModel
from bot.filters.foodai_enabled import FoodAIEnabledFilter
from bot.services.foodai import analyze_photo, analyze_text

router = Router(name="foodai")
router.message.filter(FoodAIEnabledFilter())


@router.message(F.photo)
async def handle_food_photo(message: types.Message) -> None:
    if not message.from_user:
        return

    user_id = message.from_user.id
    photos = message.photo or []
    if not photos:
        await message.answer(_("Не удалось получить фото. Пришли ещё раз, пожалуйста."))
        return

    best = photos[-1]
    tg_file_id = best.file_id
    tg_file_unique_id = best.file_unique_id

    # Step 1: persist draft meal + photo
    async with sessionmaker() as session:
        meal = MealModel(
            user_id=user_id,
            source="photo",
            status="draft",
        )
        session.add(meal)
        await session.flush()  # to get meal.id

        session.add(
            MealPhotoModel(
                meal_id=meal.id,
                tg_file_id=tg_file_id,
                tg_file_unique_id=tg_file_unique_id,
                width=best.width,
                height=best.height,
            )
        )
        await session.commit()
        meal_id = meal.id

    # Notify user we're analyzing
    analyzing_msg = await message.answer(_("Анализирую фото…"))

    # Step 2: analyze via stub service
    try:
        result = await analyze_photo(tg_file_id)
    except Exception as e:
        logger.exception("FoodAI analyze_photo failed: {}", e)
        await analyzing_msg.edit_text(_("Не удалось проанализировать фото. Попробуй ещё раз позже."))
        return

    calories = int(result.get("calories") or 0)
    protein_g = float(result.get("protein_g") or 0)
    fat_g = float(result.get("fat_g") or 0)
    carbs_g = float(result.get("carbs_g") or 0)
    weight_g = float(result.get("weight_g") or 0)
    confidence = float(result.get("confidence") or 0)
    items = list(result.get("items") or [])
    references = result.get("references") or {"source": "stub"}

    # Step 3: update Meal and DailyIntake
    today_utc = datetime.now(timezone.utc).date()
    async with sessionmaker() as session:
        # Update meal
        meal = await session.get(MealModel, meal_id)
        if meal is None:
            await analyzing_msg.edit_text(_("Не удалось сохранить результат, попробуй ещё раз."))
            return

        meal.calories = calories
        meal.protein_g = protein_g
        meal.fat_g = fat_g
        meal.carbs_g = carbs_g
        meal.weight_g = weight_g
        meal.confidence = confidence
        meal.analysis_json = result
        meal.references = references
        meal.status = "saved"

        # Add meal items if any
        for it in items:
            session.add(
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

        # Upsert daily intake
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

        di.calories = int(int(di.calories or 0) + int(calories))
        di.protein_g = float(float(di.protein_g or 0) + float(protein_g))
        di.fat_g = float(float(di.fat_g or 0) + float(fat_g))
        di.carbs_g = float(float(di.carbs_g or 0) + float(carbs_g))

        await session.commit()

    # Respond with summary
    text = _(
        "Сохранил блюдо: {cal} ккал, Б {p} г / Ж {f} г / У {c} г.\nТочность: {conf}%"
    ).format(cal=calories, p=protein_g, f=fat_g, c=carbs_g, conf=int(confidence * 100))

    await analyzing_msg.edit_text(text)


@router.message(F.text)
async def handle_food_text(message: types.Message) -> None:
    # Игнорируем команды (/start и т.п.)
    if not message.text or message.text.startswith("/"):
        return

    if not message.from_user:
        return

    user_id = message.from_user.id
    text = message.text

    # Черновик приёма пищи
    async with sessionmaker() as session:
        meal = MealModel(
            user_id=user_id,
            source="text",
            status="draft",
            title=(text[:255] if text else None),
        )
        session.add(meal)
        await session.flush()
        await session.commit()
        meal_id = meal.id

    analyzing_msg = await message.answer(_("Анализирую описание…"))

    try:
        result = await analyze_text(text)
    except Exception as e:
        logger.exception("FoodAI analyze_text failed: {}", e)
        await analyzing_msg.edit_text(_("Не удалось проанализировать текст. Попробуй ещё раз позже."))
        return

    calories = int(result.get("calories") or 0)
    protein_g = float(result.get("protein_g") or 0)
    fat_g = float(result.get("fat_g") or 0)
    carbs_g = float(result.get("carbs_g") or 0)
    weight_g = float(result.get("weight_g") or 0)
    confidence = float(result.get("confidence") or 0)
    items = list(result.get("items") or [])
    references = result.get("references") or {"source": "stub"}

    today_utc = datetime.now(timezone.utc).date()
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if meal is None:
            await analyzing_msg.edit_text(_("Не удалось сохранить результат, попробуй ещё раз."))
            return

        meal.calories = calories
        meal.protein_g = protein_g
        meal.fat_g = fat_g
        meal.carbs_g = carbs_g
        meal.weight_g = weight_g
        meal.confidence = confidence
        meal.analysis_json = result
        meal.references = references
        meal.status = "saved"

        for it in items:
            session.add(
                MealItemModel(
                    meal_id=meal.id,
                    name=str(it.get("name") or "Описание"),
                    weight_g=float(it.get("weight_g") or 0) if it.get("weight_g") is not None else None,
                    calories=float(it.get("calories") or 0) if it.get("calories") is not None else None,
                    protein_g=float(it.get("protein_g") or 0) if it.get("protein_g") is not None else None,
                    fat_g=float(it.get("fat_g") or 0) if it.get("fat_g") is not None else None,
                    carbs_g=float(it.get("carbs_g") or 0) if it.get("carbs_g") is not None else None,
                )
            )

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

        di.calories = int(int(di.calories or 0) + int(calories))
        di.protein_g = float(float(di.protein_g or 0) + float(protein_g))
        di.fat_g = float(float(di.fat_g or 0) + float(fat_g))
        di.carbs_g = float(float(di.carbs_g or 0) + float(carbs_g))

        await session.commit()

    summary = _(
        "Сохранил блюдо: {cal} ккал, Б {p} г / Ж {f} г / У {c} г.\nТочность: {conf}%"
    ).format(cal=calories, p=protein_g, f=fat_g, c=carbs_g, conf=int(confidence * 100))
    await analyzing_msg.edit_text(summary)
