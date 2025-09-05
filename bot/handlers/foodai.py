from __future__ import annotations

from datetime import datetime, timezone
import re

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import DailyIntakeModel, MealItemModel, MealModel, MealPhotoModel
from bot.filters.foodai_enabled import FoodAIEnabledFilter
from bot.services.foodai import analyze_photo, analyze_text
from bot.core.config import settings
from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.services.analytics import analytics

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

    # Step 3: update Meal only (keep draft). DailyIntake — только по кнопке Сохранить.
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
        meal.status = "draft"

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

        await session.commit()

    await analyzing_msg.edit_text(_("Готово. Предпросмотр ниже."))
    await message.answer_photo(
        tg_file_id,
        caption=_build_preview_text(calories, protein_g, fat_g, carbs_g, confidence),
        reply_markup=_preview_kb(meal_id),
    )

    # Analytics: preview shown for photo
    if analytics.logger and message.from_user:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=message.from_user.id,
                event_type="FoodAI:PreviewShown",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=f"meal_id={meal_id}, confidence={confidence}",
                    command=None,
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch="Preview", source="FoodAI", version="v1"),
            )
        )


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
        meal.status = "draft"

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

        await session.commit()

    await analyzing_msg.edit_text(_("Готово. Предпросмотр ниже."))
    await message.answer(
        _build_preview_text(calories, protein_g, fat_g, carbs_g, confidence),
        reply_markup=_preview_kb(meal_id),
    )

    # Analytics: preview shown for text
    if analytics.logger and message.from_user:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=message.from_user.id,
                event_type="FoodAI:PreviewShown",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=f"meal_id={meal_id}, confidence={confidence}",
                    command=None,
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch="Preview", source="FoodAI", version="v1"),
            )
        )


# ===== Helpers and callbacks =====

def _build_preview_text(cal: int, p: float, f: float, c: float, conf: float) -> str:
    parts = [
        _("Предпросмотр блюда:"),
        _("{cal} ккал, Б {p} г / Ж {f} г / У {c} г").format(cal=int(cal), p=p, f=f, c=c),
        _("Точность: {conf}%").format(conf=int(float(conf) * 100)),
    ]
    if float(conf) < float(settings.FOODAI_CONFIDENCE_ESCALATE):
        parts.append(_("Внимание: низкая уверенность. Рекомендуем отредактировать перед сохранением."))
    return "\n".join(parts)


def _preview_kb(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_("Сохранить"), callback_data=f"foodai:save:{meal_id}")],
            [InlineKeyboardButton(text=_("Редактировать"), callback_data=f"foodai:edit:{meal_id}")],
            [InlineKeyboardButton(text=_("Удалить"), callback_data=f"foodai:del:{meal_id}")],
        ]
    )


def _edit_kb(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="-50 ккал", callback_data=f"foodai:adj:cal:-50:{meal_id}"),
                InlineKeyboardButton(text="+50 ккал", callback_data=f"foodai:adj:cal:50:{meal_id}"),
            ],
            [
                InlineKeyboardButton(text="-10 г", callback_data=f"foodai:adj:wt:-10:{meal_id}"),
                InlineKeyboardButton(text="+10 г", callback_data=f"foodai:adj:wt:10:{meal_id}"),
            ],
            [InlineKeyboardButton(text=_("Сохранить"), callback_data=f"foodai:save:{meal_id}")],
            [InlineKeyboardButton(text=_("Назад"), callback_data=f"foodai:back:{meal_id}")],
            [InlineKeyboardButton(text=_("Удалить"), callback_data=f"foodai:del:{meal_id}")],
        ]
    )


async def _edit_caption_or_text(cb: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await cb.message.edit_caption(caption=text, reply_markup=kb)
    except Exception:
        await cb.message.edit_text(text=text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^foodai:save:(\d+)$"))
async def cb_foodai_save(callback: types.CallbackQuery) -> None:
    m = re.match(r"^foodai:save:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    meal_id = int(m.group(1))
    user_id = callback.from_user.id

    today_utc = datetime.now(timezone.utc).date()
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return

        if meal.status == "saved":
            await callback.answer(_("Уже сохранено"))
            return

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

        di.calories = int(int(di.calories or 0) + int(meal.calories or 0))
        di.protein_g = float(float(di.protein_g or 0) + float(meal.protein_g or 0))
        di.fat_g = float(float(di.fat_g or 0) + float(meal.fat_g or 0))
        di.carbs_g = float(float(di.carbs_g or 0) + float(meal.carbs_g or 0))

        meal.status = "saved"
        # Cache values before session closes to avoid accessing detached ORM instance
        _cal = int(meal.calories or 0)
        _p = float(meal.protein_g or 0)
        _f = float(meal.fat_g or 0)
        _c = float(meal.carbs_g or 0)
        _conf = float(meal.confidence or 0)
        await session.commit()

    # Analytics: save clicked
    if analytics.logger and callback.from_user:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=callback.from_user.id,
                event_type="FoodAI:SaveClicked",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"meal_id={meal_id}, cal={_cal}, p={_p}, f={_f}, c={_c}, conf={_conf}",
                    command=None,
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch="Save", source="FoodAI", version="v1"),
            )
        )

    await _edit_caption_or_text(
        callback,
        _("Сохранено: {cal} ккал, Б {p} г / Ж {f} г / У {c} г.").format(cal=_cal, p=_p, f=_f, c=_c),
        kb=None,
    )
    await callback.answer(_("Сохранено"))


@router.callback_query(F.data.regexp(r"^foodai:del:(\d+)$"))
async def cb_foodai_delete(callback: types.CallbackQuery) -> None:
    m = re.match(r"^foodai:del:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    meal_id = int(m.group(1))
    user_id = callback.from_user.id

    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return
        meal.status = "deleted"
        await session.commit()

    # Analytics: delete clicked
    if analytics.logger and callback.from_user:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=callback.from_user.id,
                event_type="FoodAI:DeleteClicked",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"meal_id={meal_id}",
                    command=None,
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch="Delete", source="FoodAI", version="v1"),
            )
        )

    await _edit_caption_or_text(callback, _("Еда удалена"), kb=None)
    await callback.answer(_("Еда удалена"))


@router.callback_query(F.data.regexp(r"^foodai:edit:(\d+)$"))
async def cb_foodai_edit(callback: types.CallbackQuery) -> None:
    m = re.match(r"^foodai:edit:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    meal_id = int(m.group(1))
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return
        text = _build_preview_text(int(meal.calories or 0), float(meal.protein_g or 0), float(meal.fat_g or 0), float(meal.carbs_g or 0), float(meal.confidence or 0))

    # Analytics: edit clicked
    if analytics.logger and callback.from_user:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=callback.from_user.id,
                event_type="FoodAI:EditClicked",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"meal_id={meal_id}",
                    command=None,
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch="Edit", source="FoodAI", version="v1"),
            )
        )

    await _edit_caption_or_text(callback, text, kb=_edit_kb(meal_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^foodai:back:(\d+)$"))
async def cb_foodai_back(callback: types.CallbackQuery) -> None:
    m = re.match(r"^foodai:back:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    meal_id = int(m.group(1))
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return
        text = _build_preview_text(int(meal.calories or 0), float(meal.protein_g or 0), float(meal.fat_g or 0), float(meal.carbs_g or 0), float(meal.confidence or 0))

    # Analytics: back clicked
    if analytics.logger and callback.from_user:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=callback.from_user.id,
                event_type="FoodAI:BackClicked",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"meal_id={meal_id}",
                    command=None,
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch="Back", source="FoodAI", version="v1"),
            )
        )

    await _edit_caption_or_text(callback, text, kb=_preview_kb(meal_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^foodai:adj:(cal|wt):(-?\d+):(\d+)$"))
async def cb_foodai_adjust(callback: types.CallbackQuery) -> None:
    m = re.match(r"^foodai:adj:(cal|wt):(-?\d+):(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    field, delta_s, meal_id_s = m.group(1), m.group(2), m.group(3)
    meal_id = int(meal_id_s)
    delta = int(delta_s)
    user_id = callback.from_user.id

    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return

        if meal.status == "saved":
            await callback.answer(_("Уже сохранено, редактирование недоступно"), show_alert=True)
            return

        if field == "cal":
            new_cal = max(0, int(meal.calories or 0) + delta)
            meal.calories = new_cal
        elif field == "wt":
            new_w = max(0, int(float(meal.weight_g or 0)) + delta)
            meal.weight_g = float(new_w)

        await session.commit()

    # Analytics for adjust
    if analytics.logger and callback.from_user:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=callback.from_user.id,
                event_type=("FoodAI:AdjustCal" if field == "cal" else "FoodAI:AdjustWt"),
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"meal_id={meal_id}, delta={delta}",
                    command=None,
                ),
                language=getattr(callback.from_user, 'language_code', None),
                plan=Plan(branch=("AdjustCal" if field == "cal" else "AdjustWt"), source="FoodAI", version="v1"),
            )
        )

    text = _build_preview_text(
        int(meal.calories or 0), float(meal.protein_g or 0), float(meal.fat_g or 0), float(meal.carbs_g or 0), float(meal.confidence or 0)
    )

    await _edit_caption_or_text(callback, text, kb=_edit_kb(meal_id))
    await callback.answer()
