from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
import re

from aiogram import F, Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import DailyIntakeModel, MealItemModel, MealModel, MealPhotoModel, OnboardingAnswerModel
from bot.filters.foodai_enabled import FoodAIEnabledFilter
from bot.services.foodai import analyze_photo, analyze_text
from bot.core.config import settings
from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.services.analytics import analytics
from bot.handlers.metrics import (
    foodai_started,
    foodai_succeeded,
    foodai_failed,
    foodai_duration_ms,
    foodai_itogo_shown,
)

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

    # Analytics: photo analyze started
    # Prometheus: started
    try:
        foodai_started.labels(source="photo").inc()
    except Exception:
        pass
    if analytics.logger and message.from_user:
        analytics.fire_event(
            BaseEvent(
                user_id=message.from_user.id,
                event_type="FoodAI:PhotoAnalyzeStarted",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=f"file_id_len={len(tg_file_id)}",
                    command=None,
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
            )
        )

    # Step 2: analyze via service
    t0 = perf_counter()
    try:
        result = await analyze_photo(tg_file_id)
    except Exception as e:
        logger.exception("FoodAI analyze_photo failed: {}", e)
        # Prometheus: failed
        try:
            foodai_failed.labels(source="photo").inc()
        except Exception:
            pass
        # Analytics: photo analyze failed
        if analytics.logger and message.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=message.from_user.id,
                    event_type="FoodAI:PhotoAnalyzeFailed",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text=str(e),
                        command=None,
                    ),
                    language=message.from_user.language_code if message.from_user else None,
                    plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
                )
            )
        await analyzing_msg.edit_text(_("Не удалось проанализировать фото. Попробуй ещё раз позже."))
        return
    finally:
        pass

    title = (result.get("title") or None) if isinstance(result, dict) else None
    calories = int(result.get("calories") or 0)
    protein_g = float(result.get("protein_g") or 0)
    fat_g = float(result.get("fat_g") or 0)
    carbs_g = float(result.get("carbs_g") or 0)
    weight_g = float(result.get("weight_g") or 0)
    confidence = float(result.get("confidence") or 0)
    items = list(result.get("items") or [])
    references = result.get("references") or {"source": "stub"}
    analysis_text = (result.get("analysis_text") or None)

    # Step 3: update Meal only (keep draft). DailyIntake — только по кнопке Сохранить.
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if meal is None:
            await analyzing_msg.edit_text(_("Не удалось сохранить результат, попробуй ещё раз."))
            return

        meal.title = title
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

    # Prepare optional per-meal % of plan for preview
    itogo = None
    try:
        async with sessionmaker() as session:
            oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
            if oa and isinstance(getattr(oa, "daily_plan", None), dict):
                plan = oa.daily_plan or {}
                plan_cal = float(plan.get("calories") or 0)
                plan_p = float(plan.get("protein_g") or 0)
                plan_f = float(plan.get("fat_g") or 0)
                plan_c = float(plan.get("carbs_g") or 0)
                def pct(val: float, base: float) -> float:
                    try:
                        if base and base > 0:
                            return round(100.0 * float(val) / float(base), 1)
                        return 0.0
                    except Exception:
                        return 0.0
                itogo = {
                    "p_pct": pct(protein_g, plan_p),
                    "f_pct": pct(fat_g, plan_f),
                    "c_pct": pct(carbs_g, plan_c),
                    "cal_pct": pct(calories, plan_cal),
                }
    except Exception:
        itogo = None

    # Remove status message
    try:
        await analyzing_msg.delete()
    except Exception:
        pass
    preview_text = _build_preview_text(
        calories,
        protein_g,
        fat_g,
        carbs_g,
        confidence,
        weight=weight_g,
        items=items,
        references=references,
        title=title,
        source="photo",
        itogo=itogo,
        analysis_text=analysis_text,
    )
    # Do NOT resend the photo; just send full preview as a separate text message with inline keyboard
    await message.answer(preview_text, reply_markup=_preview_kb(meal_id))

    # Prometheus: succeeded + duration
    try:
        try:
            _dur_ms = int((perf_counter() - t0) * 1000)
        except Exception:
            _dur_ms = None
        foodai_succeeded.labels(source="photo").inc()
        if itogo:
            try:
                foodai_itogo_shown.labels(source="photo").inc()
            except Exception:
                pass
        if _dur_ms is not None:
            foodai_duration_ms.observe(_dur_ms)
    except Exception:
        pass

    # Analytics: preview shown for photo
    if analytics.logger and message.from_user:
        # Photo analyze succeeded (duration + confidence)
        try:
            dur_ms = int((perf_counter() - t0) * 1000)
        except Exception:
            dur_ms = None
        analytics.fire_event(
            BaseEvent(
                user_id=message.from_user.id,
                event_type="FoodAI:PhotoAnalyzeSucceeded",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=f"meal_id={meal_id}, confidence={confidence}, dur_ms={dur_ms}",
                    command=None,
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
            )
        )
        analytics.fire_event(
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

    # Analytics: text analyze started
    # Prometheus: started
    try:
        foodai_started.labels(source="text").inc()
    except Exception:
        pass
    if analytics.logger and message.from_user:
        analytics.fire_event(
            BaseEvent(
                user_id=message.from_user.id,
                event_type="FoodAI:TextAnalyzeStarted",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=f"len={len(text or '')}",
                    command=None,
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
            )
        )

    t0 = perf_counter()
    try:
        result = await analyze_text(text)
    except Exception as e:
        logger.exception("FoodAI analyze_text failed: {}", e)
        # Prometheus: failed
        try:
            foodai_failed.labels(source="text").inc()
        except Exception:
            pass
        # Analytics: text analyze failed
        if analytics.logger and message.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=message.from_user.id,
                    event_type="FoodAI:TextAnalyzeFailed",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text=str(e),
                        command=None,
                    ),
                    language=message.from_user.language_code if message.from_user else None,
                    plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
                )
            )
        await analyzing_msg.edit_text(_("Не удалось проанализировать текст. Попробуй ещё раз позже."))
        return
    finally:
        pass

    title = (result.get("title") or None) if isinstance(result, dict) else None
    calories = int(result.get("calories") or 0)
    protein_g = float(result.get("protein_g") or 0)
    fat_g = float(result.get("fat_g") or 0)
    carbs_g = float(result.get("carbs_g") or 0)
    weight_g = float(result.get("weight_g") or 0)
    confidence = float(result.get("confidence") or 0)
    items = list(result.get("items") or [])
    references = result.get("references") or {"source": "stub"}
    analysis_text = (result.get("analysis_text") or None)

    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if meal is None:
            await analyzing_msg.edit_text(_("Не удалось сохранить результат, попробуй ещё раз."))
            return

        meal.title = title or meal.title
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

    # Prepare optional per-meal % of plan for preview
    itogo = None
    try:
        async with sessionmaker() as session:
            oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
            if oa and isinstance(getattr(oa, "daily_plan", None), dict):
                plan = oa.daily_plan or {}
                plan_cal = float(plan.get("calories") or 0)
                plan_p = float(plan.get("protein_g") or 0)
                plan_f = float(plan.get("fat_g") or 0)
                plan_c = float(plan.get("carbs_g") or 0)
                def pct(val: float, base: float) -> float:
                    try:
                        if base and base > 0:
                            return round(100.0 * float(val) / float(base), 1)
                        return 0.0
                    except Exception:
                        return 0.0
                itogo = {
                    "p_pct": pct(protein_g, plan_p),
                    "f_pct": pct(fat_g, plan_f),
                    "c_pct": pct(carbs_g, plan_c),
                    "cal_pct": pct(calories, plan_cal),
                }
    except Exception:
        itogo = None

    # Remove status message; just clean up the temporary "analyzing" message
    try:
        await analyzing_msg.delete()
    except Exception:
        pass
    preview_text = _build_preview_text(
        calories,
        protein_g,
        fat_g,
        carbs_g,
        confidence,
        weight=weight_g,
        items=items,
        references=references,
        title=title or (text[:255] if text else None),
        source="text",
        itogo=itogo,
        analysis_text=analysis_text,
    )
    # Send full preview as a separate text message with inline keyboard
    await message.answer(preview_text, reply_markup=_preview_kb(meal_id))

    # Prometheus: succeeded + duration
    try:
        try:
            _dur_ms = int((perf_counter() - t0) * 1000)
        except Exception:
            _dur_ms = None
        foodai_succeeded.labels(source="text").inc()
        if itogo:
            try:
                foodai_itogo_shown.labels(source="text").inc()
            except Exception:
                pass
        if _dur_ms is not None:
            foodai_duration_ms.observe(_dur_ms)
    except Exception:
        pass

    # Analytics: preview shown for text
    if analytics.logger and message.from_user:
        # Text analyze succeeded
        try:
            dur_ms = int((perf_counter() - t0) * 1000)
        except Exception:
            dur_ms = None
        analytics.fire_event(
            BaseEvent(
                user_id=message.from_user.id,
                event_type="FoodAI:TextAnalyzeSucceeded",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=f"meal_id={meal_id}, confidence={confidence}, dur_ms={dur_ms}",
                    command=None,
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
            )
        )
        analytics.fire_event(
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

def _build_preview_text(
    cal: int,
    p: float,
    f: float,
    c: float,
    conf: float,
    *,
    weight: float | None = None,
    items: list | None = None,
    references: dict | None = None,
    title: str | None = None,
    source: str | None = None,
    itogo: dict | None = None,
    analysis_text: str | None = None,
) -> str:
    parts: list[str] = []
    # Header by source (aligned to AIFood.md wording)
    if source == "photo":
        parts.append(_("👌🏼 Анализ фото готов !"))
    elif source == "text":
        parts.append(_("📝 Анализ описания завершен!"))
    else:
        parts.append(_("Предпросмотр блюда:"))

    if title:
        parts.append(str(title))

    # Composition
    if items:
        parts.append("")
        parts.append(_("🍜 Состав:"))
        for it in items:
            name = str((it or {}).get("name") or _("Блюдо"))
            w = (it or {}).get("weight_g")
            kc = (it or {}).get("calories")
            if w or kc is not None:
                segs = []
                if w:
                    try:
                        segs.append(f"{float(w):g} г")
                    except Exception:
                        segs.append(str(w))
                if kc is not None:
                    try:
                        segs.append(f"{int(float(kc))} ккал")
                    except Exception:
                        segs.append(str(kc))
                parts.append(f"• {name} (" + ", ".join(segs) + ")")
            else:
                parts.append(f"• {name}")

    # Totals in one line
    parts.append("")
    parts.append(
        _("🔥 Калории: {cal} ккал | 🥩 Белки: {p} г | 🥑 Жиры: {f} г | 🍞 Углеводы: {c} г").format(
            cal=int(cal), p=p, f=f, c=c
        )
    )

    if weight:
        parts.append("")
        parts.append(_("⚖️ Вес: {w} г").format(w=weight))

    # Separator
    parts.append("")
    parts.append("------------------------------")

    # Sources
    parts.append("")
    parts.append(_("📋 Источники данных:"))
    parts.append("• ФГБУН ФИЦ питания и биотехнологии")
    parts.append("• USDA FoodData Central")

    # Analysis
    parts.append("")
    parts.append(_("🔍 Анализ:"))
    if analysis_text:
        try:
            txt = str(analysis_text).replace("\n", " ").replace("\r", " ").strip()
            if txt:
                parts.append(txt)
        except Exception:
            pass
    parts.append(_("Уровень уверенности {conf}%").format(conf=int(float(conf) * 100)))
    if float(conf) < float(settings.FOODAI_CONFIDENCE_ESCALATE):
        parts.append(_("Внимание: низкая уверенность. Рекомендуем отредактировать перед сохранением."))

    # Optional per-meal percent of plan
    if isinstance(itogo, dict):
        parts.append("")
        parts.append(_("📊 Итого:"))
        try:
            parts.append(_("🥩 Белки: {v} г ({pct}% от нормы)").format(v=p, pct=itogo.get("p_pct") or 0))
            parts.append(_("🥑 Жиры: {v} г ({pct}% от нормы)").format(v=f, pct=itogo.get("f_pct") or 0))
            parts.append(_("🍞 Углеводы: {v} г ({pct}% от нормы)").format(v=c, pct=itogo.get("c_pct") or 0))
            parts.append(_("🔥 Калории: {v} ккал ({pct}% от нормы)").format(v=int(cal), pct=itogo.get("cal_pct") or 0))
        except Exception:
            pass

    return "\n".join(parts)


def _preview_kb(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_("✅ Сохранить"), callback_data=f"foodai:save:{meal_id}")],
            [
                InlineKeyboardButton(text=_("✏️ Редактировать"), callback_data=f"foodai:edit:{meal_id}"),
                InlineKeyboardButton(text=_("🗑 Удалить"), callback_data=f"foodai:del:{meal_id}"),
            ],
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
            [InlineKeyboardButton(text=_("✅ Сохранить"), callback_data=f"foodai:save:{meal_id}")],
            [InlineKeyboardButton(text=_("◀️ Назад"), callback_data=f"foodai:back:{meal_id}")],
            [InlineKeyboardButton(text=_("🗑 Удалить"), callback_data=f"foodai:del:{meal_id}")],
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
        analytics.fire_event(
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

    # Build combined message: Saved line + Day analysis in one message
    saved_line = _("✅ Еда сохранена")
    analysis_text = ""
    try:
        async with sessionmaker() as session:
            di = await session.scalar(
                select(DailyIntakeModel).where(
                    (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc == today_utc)
                )
            )
            oa = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
            )

        if di and oa and isinstance(getattr(oa, "daily_plan", None), dict):
            plan = oa.daily_plan or {}
            plan_cal = int(plan.get("calories") or 0)
            plan_p = float(plan.get("protein_g") or 0)
            plan_f = float(plan.get("fat_g") or 0)
            plan_c = float(plan.get("carbs_g") or 0)

            fact_cal = int(di.calories or 0)
            fact_p = float(di.protein_g or 0)
            fact_f = float(di.fat_g or 0)
            fact_c = float(di.carbs_g or 0)

            diff_cal = plan_cal - fact_cal
            diff_p = plan_p - fact_p
            diff_f = plan_f - fact_f
            diff_c = plan_c - fact_c

            def _fmt(delta: float, emoji: str, unit: str) -> str:
                if unit == "ккал":
                    val = int(abs(delta))
                else:
                    val = round(abs(delta), 1)
                if delta > 0:
                    return f"{emoji} {val} {unit} до нормы"
                if delta < 0:
                    return f"⚠️ {emoji} +{val} {unit} превышено"
                return f"{emoji} норма достигнута"

            lines = [
                _("Анализ дня:"),
                _fmt(diff_cal, "🔥", "ккал"),
                _fmt(diff_p, "🥩", "г"),
                _fmt(diff_f, "🥑", "г"),
                _fmt(diff_c, "🍞", "г"),
            ]
            analysis_text = "\n".join(lines)
    except Exception as e:
        logger.warning("day_analysis_failed | user_id={} | err={}", user_id, e)

    combined_text = saved_line if not analysis_text else f"{saved_line}\n\n{analysis_text}"
    await _edit_caption_or_text(callback, combined_text, kb=None)
    await callback.answer(_("✅ Еда сохранена"))


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
        analytics.fire_event(
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

    await _edit_caption_or_text(callback, _("🗑 Еда удалена"), kb=None)
    await callback.answer(_("🗑 Еда удалена"))


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
        # Build items list safely
        items = []
        try:
            for it in (meal.items or []):
                items.append({"name": it.name})
        except Exception:
            items = []
        text = _build_preview_text(
            int(meal.calories or 0),
            float(meal.protein_g or 0),
            float(meal.fat_g or 0),
            float(meal.carbs_g or 0),
            float(meal.confidence or 0),
            weight=float(meal.weight_g or 0),
            items=items,
            references=meal.references or None,
            title=meal.title or None,
            source=meal.source or None,
        )

    # Analytics: edit clicked
    if analytics.logger and callback.from_user:
        analytics.fire_event(
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
        items = []
        try:
            for it in (meal.items or []):
                items.append({"name": it.name})
        except Exception:
            items = []
        text = _build_preview_text(
            int(meal.calories or 0),
            float(meal.protein_g or 0),
            float(meal.fat_g or 0),
            float(meal.carbs_g or 0),
            float(meal.confidence or 0),
            weight=float(meal.weight_g or 0),
            items=items,
            references=meal.references or None,
            title=meal.title or None,
            source=meal.source or None,
        )

    # Analytics: back clicked
    if analytics.logger and callback.from_user:
        analytics.fire_event(
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
        analytics.fire_event(
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

    # Cache values and items before session closes
    cal_v = int(meal.calories or 0)
    p_v = float(meal.protein_g or 0)
    f_v = float(meal.fat_g or 0)
    c_v = float(meal.carbs_g or 0)
    conf_v = float(meal.confidence or 0)
    weight_v = float(meal.weight_g or 0)
    title_v = meal.title or None
    source_v = meal.source or None
    items_v = []
    try:
        for it in (meal.items or []):
            items_v.append({"name": it.name})
    except Exception:
        items_v = []
    refs_v = meal.references or None

    text = _build_preview_text(
        cal_v,
        p_v,
        f_v,
        c_v,
        conf_v,
        weight=weight_v,
        items=items_v,
        references=refs_v,
        title=title_v,
        source=source_v,
    )

    await _edit_caption_or_text(callback, text, kb=_edit_kb(meal_id))
    await callback.answer()
