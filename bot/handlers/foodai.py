from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
import re
from html import escape as _html_escape

from aiogram import Router, types, F
from aiogram.filters import CommandStart
from aiogram.filters import StateFilter
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import DailyIntakeModel, MealItemModel, MealModel, MealPhotoModel, OnboardingAnswerModel
from bot.filters.foodai_enabled import FoodAIEnabledFilter
from bot.services.foodai import analyze_photo, analyze_text, refine_meal
from bot.core.config import settings
from bot.handlers.start import start_handler  # to forward /start from edit-state
from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.services.analytics import analytics
from bot.handlers.metrics import (
    foodai_started,
    foodai_succeeded,
    foodai_failed,
    foodai_duration_ms,
    foodai_itogo_shown,
    foodai_not_food,
    # Edit flow
    foodai_edit_started,
    foodai_edit_applied,
    foodai_edit_failed,
    foodai_edit_duration_ms,
)

router = Router(name="foodai")
router.message.filter(FoodAIEnabledFilter())
# Do not process ANY FoodAI messages while user is in any FSM state (e.g., onboarding)
router.message.filter(StateFilter(None))
# Apply same constraints to callbacks to ignore old buttons during onboarding and restrict to enabled users
router.callback_query.filter(FoodAIEnabledFilter())
router.callback_query.filter(StateFilter(None))


# Separate router for edit text state (does not have global StateFilter(None))
router_edit = Router(name="foodai_edit")
router_edit.message.filter(FoodAIEnabledFilter())
router_edit.callback_query.filter(FoodAIEnabledFilter())


class EditStates(StatesGroup):
    """FSM states for FoodAI edit flow."""
    waiting_text = State()


# If user sends /start while in edit-state, clear FSM and forward to normal start handler
@router_edit.message(CommandStart())
async def edit_catch_start(message: types.Message, state: FSMContext) -> None:
    try:
        await state.clear()
    except Exception:
        pass
    await start_handler(message, state)


# Edit-state: accept only plain text (exclude commands like /start)
@router_edit.message(StateFilter(EditStates.waiting_text), F.text & (~F.text.startswith("/")))
async def edit_text_received(message: types.Message, state: FSMContext) -> None:
    t0 = perf_counter()
    if not message.from_user:
        return
    user_id = message.from_user.id
    data = await state.get_data()
    meal_id = int(data.get("edit_meal_id") or 0)
    if not meal_id:
        await state.clear()
        await message.answer(_("Сессия редактирования завершилась. Нажмите Редактировать ещё раз."))
        return

    # Load current meal
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await state.clear()
            await message.answer(_("Не найдено"))
            return
        # Prepare base for refinement
        base = {
            "title": meal.title or "",
            "calories": int(meal.calories or 0),
            "protein_g": float(meal.protein_g or 0),
            "fat_g": float(meal.fat_g or 0),
            "carbs_g": float(meal.carbs_g or 0),
            "weight_g": float(meal.weight_g or 0),
            "items": [
                {
                    "name": it.name,
                    "weight_g": float(it.weight_g) if it.weight_g is not None else None,
                    "calories": float(it.calories) if it.calories is not None else None,
                    "protein_g": float(it.protein_g) if it.protein_g is not None else None,
                    "fat_g": float(it.fat_g) if it.fat_g is not None else None,
                    "carbs_g": float(it.carbs_g) if it.carbs_g is not None else None,
                }
                for it in (meal.items or [])
            ],
            "source": meal.source or None,
        }

    instruction = (message.text or "").strip()

    # Analytics: Submitted
    if analytics.logger and message.from_user:
        analytics.fire_event(
            BaseEvent(
                user_id=message.from_user.id,
                event_type="FoodAI:EditSubmitted",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=f"meal_id={meal_id}, len={len(instruction)}",
                    command=None,
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch="Edit", source="FoodAI", version="v1"),
            )
        )

    # Call refinement service
    try:
        result = await refine_meal(base, instruction)
    except Exception as e:
        result = {"error": str(e), "meta": {"action": "unknown", "reason": "other"}}

    # Metrics and failure handling
    meta = result.get("meta") if isinstance(result, dict) else None
    action = (meta or {}).get("action") or "unknown"
    reason = (meta or {}).get("reason") or ("error" if result.get("error") else None)
    try:
        foodai_edit_started.labels(action=action).inc()
    except Exception:
        pass

    if not isinstance(result, dict) or result.get("error"):
        # Metrics fail
        try:
            foodai_edit_failed.labels(action=action, reason=(reason or "other")).inc()
            foodai_edit_duration_ms.observe(max(0.0, (perf_counter() - t0) * 1000.0))
        except Exception:
            pass
        # Amplitude fail
        if analytics.logger and message.from_user:
            try:
                analytics.fire_event(
                    BaseEvent(
                        user_id=message.from_user.id,
                        event_type="FoodAI:EditFailed",
                        event_properties=EventProperties(
                            chat_id=message.chat.id if message.chat else None,
                            chat_type=message.chat.type if message.chat else None,
                            text=f"meal_id={meal_id}, action={action}, reason={reason}",
                            command=None,
                        ),
                        language=message.from_user.language_code if message.from_user else None,
                        plan=Plan(branch="Edit", source="FoodAI", version="v1"),
                    )
                )
            except Exception:
                pass
        # Build contextual ingredient list for better hints
        ing_hint = None
        try:
            async with sessionmaker() as session:
                meal_ctx = await session.get(MealModel, meal_id)
                if meal_ctx:
                    names: list[str] = []
                    for it in (meal_ctx.items or []):
                        try:
                            n = (it.name or "").strip()
                        except Exception:
                            n = None
                        if n:
                            names.append(n)
                    if names:
                        ing_hint = _("Состав сейчас: {lst}").format(lst=", ".join(names[:8]))
        except Exception:
            ing_hint = None

        # User message by reason (more actionable)
        base_map = {
            "parse": _("Не понял запрос. Примеры: добавить сыр 30 г; убрать соус; заменить рыбу на индейку 100 г; увеличить порцию на 20%."),
            "ambiguous": _("Нашёл несколько совпадений. Уточните точнее название, например: заменить соус томатный на кетчуп 20 г."),
            "not_found": _("Ингредиент не найден в составе. Напишите так, как в списке ниже, или уточните форму: например, 'замени куриное филе на рыбу 100 г'."),
            "caps": _("Слишком большая масса. Ограничение — до 1000 г/мл."),
            "unsupported": _("Пока не поддерживаю такой запрос. Попробуйте: добавить/убрать/заменить/изменить массу/увеличить порцию. Для добавления без граммов поставьте: 'добавь базилик 5 г'."),
            "not_food": _("Похоже, это не еда и не напиток. Если это специя/зелень — укажите массу, например: 'петрушка 5 г'."),
        }
        msg = base_map.get(reason or "", _("Не удалось применить изменения. Попробуйте переформулировать и отправьте ещё раз."))
        if ing_hint and (reason in {"ambiguous", "not_found"}):
            msg = msg + "\n\n" + ing_hint
        await message.answer(msg)
        return

    # Persist updated meal
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await state.clear()
            await message.answer(_("Не найдено"))
            return
        meal.title = (result.get("title") or meal.title)
        meal.calories = int(result.get("calories") or 0)
        meal.protein_g = float(result.get("protein_g") or 0)
        meal.fat_g = float(result.get("fat_g") or 0)
        meal.carbs_g = float(result.get("carbs_g") or 0)
        meal.weight_g = float(result.get("weight_g") or 0)
        meal.status = "draft"
        # Replace items
        try:
            for it in list(meal.items or []):
                await session.delete(it)
        except Exception:
            pass
        for it in (result.get("items") or []):
            session.add(
                MealItemModel(
                    meal_id=meal.id,
                    name=str(it.get("name") or "Ингредиент"),
                    weight_g=float(it.get("weight_g") or 0) if it.get("weight_g") is not None else None,
                    calories=float(it.get("calories") or 0) if it.get("calories") is not None else None,
                    protein_g=float(it.get("protein_g") or 0) if it.get("protein_g") is not None else None,
                    fat_g=float(it.get("fat_g") or 0) if it.get("fat_g") is not None else None,
                    carbs_g=float(it.get("carbs_g") or 0) if it.get("carbs_g") is not None else None,
                )
            )
        await session.commit()

    # Metrics success
    try:
        foodai_edit_applied.labels(action=action).inc()
        foodai_edit_duration_ms.observe(max(0.0, (perf_counter() - t0) * 1000.0))
    except Exception:
        pass

    # Amplitude success
    if analytics.logger and message.from_user:
        try:
            delta_cal = None
            try:
                delta_cal = int((result.get("meta") or {}).get("delta_cal"))
            except Exception:
                delta_cal = None
            analytics.fire_event(
                BaseEvent(
                    user_id=message.from_user.id,
                    event_type="FoodAI:EditApplied",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text=f"meal_id={meal_id}, action={action}, delta_cal={delta_cal}, dur_ms={int((perf_counter()-t0)*1000)}",
                        command=None,
                    ),
                    language=message.from_user.language_code if message.from_user else None,
                    plan=Plan(branch="Edit", source="FoodAI", version="v1"),
                )
            )
        except Exception:
            pass

    # Clear state and show updated preview
    await state.clear()
    cal = int(result.get("calories") or 0)
    p = float(result.get("protein_g") or 0)
    f = float(result.get("fat_g") or 0)
    c = float(result.get("carbs_g") or 0)
    w = float(result.get("weight_g") or 0)
    items = list(result.get("items") or [])
    title = (result.get("title") or meal.title)
    text_preview = _build_preview_text(cal, p, f, c, 0.8, weight=w, items=items, references=None, title=title, source="edit")
    combined = "\n" + _("👍🏼  Готово !") + "\n" + _("📝 Изменения: {t}").format(t=instruction) + "\n\n" + text_preview
    await message.answer(combined, reply_markup=_preview_kb(meal_id))


# Edit-state: reject non-text input (photos, stickers, etc.)
@router_edit.message(StateFilter(EditStates.waiting_text))
async def edit_non_text(message: types.Message) -> None:
    await message.answer(_("Напишите, пожалуйста, что заменить или добавить в блюдо. Например: добавить соус."))


# Edit-state specific back handler
@router_edit.callback_query(StateFilter(EditStates.waiting_text), F.data.regexp(r"^foodai:back:(\d+)$"))
async def cb_foodai_back_edit(callback: types.CallbackQuery, state: FSMContext) -> None:
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
    await state.clear()
    await _edit_caption_or_text(callback, text, kb=_preview_kb(meal_id))
    await callback.answer()


@router.message(F.photo)
async def handle_food_photo(message: types.Message, state: FSMContext) -> None:
    # Extra safety: ignore during any active FSM state (e.g., onboarding)
    try:
        cur = await state.get_state()
        if cur is not None:
            return
    except Exception:
        pass
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
    analyzing_msg = await message.answer(_("✨Анализирую еду на фото… это может занять около 35 секунд"))

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
        # Vision escalation started (feature flag based)
        try:
            if getattr(settings, "FOODAI_VISION_ESCALATION_ENABLED", False):
                chain = str(getattr(settings, "FOODAI_VISION_ESCALATION_CHAIN", "") or "")
                order = str(getattr(settings, "FOODAI_VISION_DETAIL_ORDER", "low>high") or "low>high")
                analytics.fire_event(
                    BaseEvent(
                        user_id=message.from_user.id,
                        event_type="FoodAI:VisionEscalationStarted",
                        event_properties=EventProperties(
                            chat_id=message.chat.id if message.chat else None,
                            chat_type=message.chat.type if message.chat else None,
                            text=f"chain={chain}, detail_order={order}",
                            command=None,
                            esc_chain=chain,
                            esc_detail_order=order,
                        ),
                        language=message.from_user.language_code if message.from_user else None,
                        plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
                    )
                )
        except Exception:
            pass

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
        await analyzing_msg.edit_text(_("Не удалось проанализировать фото. Попробуйте ещё раз позже."))
        return
    finally:
        pass

    # Provider error short-circuit (no preview on operational issues)
    try:
        if isinstance(result, dict) and result.get("error"):
            err = str(result.get("error") or "")
            msg = _("Не удалось проанализировать фото. Попробуйте ещё раз позже.")
            if err == "file_url_unavailable":
                msg = _("Не удалось получить файл с серверов Telegram. Попробуйте ещё раз.")
            await analyzing_msg.edit_text(msg)
            try:
                foodai_failed.labels(source="photo").inc()
            except Exception:
                pass
            return
    except Exception:
        pass

    # Not-food short-circuit
    try:
        if isinstance(result, dict) and bool(result.get("not_food")):
            try:
                foodai_not_food.labels(source="photo").inc()
            except Exception:
                pass
            await analyzing_msg.edit_text(_("Похоже, на изображении нет еды или напитков. Пришлите фото блюда или продукта."))
            return
    except Exception:
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
        # Vision escalation completed (if meta provided by service)
        try:
            esc = None
            try:
                esc = (result.get("meta") or {}).get("escalation") if isinstance(result, dict) else None
            except Exception:
                esc = None
            if esc:
                chain = esc.get("chain")
                final_model = esc.get("final_model")
                steps = esc.get("steps")
                reason = esc.get("stopped_reason")
                total_ms = esc.get("total_ms")
                analytics.fire_event(
                    BaseEvent(
                        user_id=message.from_user.id,
                        event_type="FoodAI:VisionEscalationCompleted",
                        event_properties=EventProperties(
                            chat_id=message.chat.id if message.chat else None,
                            chat_type=message.chat.type if message.chat else None,
                            text=f"chain={chain}, final_model={final_model}, steps={steps}, reason={reason}, total_ms={total_ms}",
                            command=None,
                            esc_chain=(" > ".join(chain) if isinstance(chain, list) else (str(chain) if chain is not None else None)),
                            esc_detail_order=(" > ".join((esc.get("detail_order") or [])) if isinstance(esc.get("detail_order"), list) else (str(esc.get("detail_order")) if esc.get("detail_order") is not None else None)),
                            esc_final_model=(str(final_model) if final_model is not None else None),
                            esc_steps=(int(steps) if steps is not None else None),
                            esc_reason=(str(reason) if reason is not None else None),
                            esc_total_ms=(int(total_ms) if total_ms is not None else None),
                        ),
                        language=message.from_user.language_code if message.from_user else None,
                        plan=Plan(branch="Analyze", source="FoodAI", version="v1"),
                    )
                )
        except Exception:
            pass
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


# Text handler: process ONLY when no FSM state is active (to not interfere with onboarding)
@router.message(StateFilter(None), F.text & (~F.text.startswith("/")), flags={"block": False})
async def handle_food_text(message: types.Message, state: FSMContext) -> None:
    # Extra safety: if ANY FSM state is active (e.g., onboarding), do nothing
    try:
        cur = await state.get_state()
        if cur is not None:
            return
    except Exception:
        pass
    # Игнорируем команды (/start и т.п.) — дополнительная защита
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

    try:
        logger.info("FoodAI:Text handler entered | user_id={} | len={}", message.from_user.id if message.from_user else None, len(message.text or ""))
    except Exception:
        pass
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

    # Provider error short-circuit (no preview on operational issues)
    try:
        if isinstance(result, dict) and result.get("error"):
            err = str(result.get("error") or "")
            msg = _("Не удалось проанализировать текст. Попробуйте ещё раз позже.")
            await analyzing_msg.edit_text(msg)
            try:
                logger.warning("FoodAI:Text provider error {} | user_id={}", err, message.from_user.id if message.from_user else None)
            except Exception:
                pass
            try:
                foodai_failed.labels(source="text").inc()
            except Exception:
                pass
            return
    except Exception:
        pass

    # Not-food short-circuit for text (ensure explicit log and early return)
    try:
        if isinstance(result, dict) and bool(result.get("not_food")):
            try:
                logger.info("FoodAI:Text not_food short-circuit | user_id={} | text_len={}", message.from_user.id if message.from_user else None, len(text or ""))
            except Exception:
                pass
            try:
                foodai_not_food.labels(source="text").inc()
            except Exception:
                pass
            await analyzing_msg.edit_text(_("Похоже, это не описание еды или напитков. Попробуйте описать блюдо или продукт."))
            return
    except Exception:
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
    # Early exit if not_food flagged
    try:
        not_food_flag = bool((itogo or {}) and False)
    except Exception:
        not_food_flag = False
    try:
        if isinstance(references, dict) and bool(references.get("not_food")):
            not_food_flag = True
    except Exception:
        pass
    try:
        if isinstance(items, dict) and bool((items or {}).get("not_food")):
            not_food_flag = True
    except Exception:
        pass
    # Some providers place not_food in analysis_json root; try finding in a shadow field
    try:
        if isinstance(items, list):
            # no-op; keep list
            pass
    except Exception:
        pass
    # Header by source (aligned to AIFood.md wording)
    if source == "photo":
        parts.append(_("👌🏼 Анализ фото готов !"))
    elif source == "text":
        parts.append(_("📝 Анализ описания завершен!"))
    elif source == "edit":
        pass  # no header
    else:
        parts.append(_("Предпросмотр блюда:"))

    if title:
        # One blank line between header and title; title in bold (HTML parse mode)
        parts.append("")
        parts.append(f"<b>{_html_escape(str(title))}</b>")

    # If not food — refuse with a short message
    if not_food_flag:
        try:
            parts.append("")
            parts.append(_("Похоже, на изображении нет еды или напитков. Пришлите фото блюда или пищевого продукта."))
            try:
                foodai_not_food.labels(source=source or "unknown").inc()
            except Exception:
                pass
            return "\n".join(parts)
        except Exception:
            return "\n".join(parts)

    # Composition
    if items:
        parts.append("")
        parts.append(_("🍜 Состав:"))
        # Simple density map for liquids (g/ml)
        density = {
            "вода": 1.0,
            "сок": 1.04,
            "кофе": 1.0,
            "чай": 1.0,
            "молоко": 1.03,
            "кефир": 1.03,
            "йогурт": 1.03,
            "бульон": 1.0,
            "суп": 1.0,
            "лимонад": 1.02,
            "масло": 0.91,
        }
        for it in items:
            name = str((it or {}).get("name") or _("Блюдо"))
            w = (it or {}).get("weight_g")
            kc = (it or {}).get("calories")
            is_liquid = bool((it or {}).get("is_liquid"))
            app = (it or {}).get("appearance") if isinstance(it, dict) else None
            if w or kc is not None:
                segs = []
                if w is not None:
                    try:
                        val_g = float(w)
                        # Prefer explicit 'appearance' from service if present
                        if isinstance(app, dict) and app.get("unit") in {"ml", "l", "шт"}:
                            u = str(app.get("unit"))
                            qty = app.get("qty")
                            approx_g = float(app.get("approx_g") or val_g)
                            # Render original units and keep grams as approx
                            if u == "ml":
                                try:
                                    q = float(qty)
                                    q_show = f"{int(q)}" if abs(q - int(q)) < 1e-6 else f"{q:g}"
                                except Exception:
                                    q_show = str(qty)
                                segs.append(f"{q_show} мл (≈{approx_g:.0f} г)")
                                is_liquid = True
                            elif u == "l":
                                try:
                                    q = float(qty)
                                    q_show = f"{q:g}"
                                except Exception:
                                    q_show = str(qty)
                                segs.append(f"{q_show} л (≈{approx_g:.0f} г)")
                                is_liquid = True
                            elif u == "шт":
                                try:
                                    q = float(qty)
                                    q_show = f"{int(q)}" if abs(q - int(q)) < 1e-6 else f"{q:g}"
                                except Exception:
                                    q_show = str(qty)
                                segs.append(f"{q_show} шт (≈{approx_g:.0f} г)")
                        else:
                            unit = "г"
                            show_val = f"{val_g:g}"
                            # Fallback: convert to ml if flagged liquid
                            if is_liquid:
                                d = None
                                key = name.lower().strip()
                                for k in density.keys():
                                    if key.startswith(k):
                                        d = density[k]
                                        break
                                if not d:
                                    d = 1.0
                                ml = val_g / d
                                unit = "мл"
                                show_val = f"{ml:.0f}"
                            segs.append(f"{show_val} {unit}")
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
    # Confidence display as category; hide for high confidence
    # Optional confidence labels (низкая/средняя). Hidden if FOODAI_SHOW_CONF_LABELS is false
    show_conf_labels = bool(getattr(settings, "FOODAI_SHOW_CONF_LABELS", True))
    if show_conf_labels:
        try:
            cval = float(conf)
            low_thr = float(getattr(settings, "FOODAI_CONF_LOW", 0.6))
            high_thr = float(getattr(settings, "FOODAI_CONF_HIGH", 0.8))
            if cval < low_thr:
                parts.append(_("Уверенность: низкая"))
            elif cval < high_thr:
                parts.append(_("Уверенность: средняя"))
            # else: high — do not show the line
        except Exception:
            # fallback: keep old numeric display on parsing issues
            try:
                parts.append(_("Уровень уверенности {conf}%").format(conf=int(float(conf) * 100)))
            except Exception:
                pass
    # Keep warning if below escalate threshold
    try:
        conf_thr = float(getattr(settings, "FOODAI_ESCALATE_CONF", getattr(settings, "FOODAI_CONFIDENCE_ESCALATE", 0.7)))
    except Exception:
        conf_thr = 0.7
    # Optional hint line controlled by FOODAI_SHOW_LOW_CONF_HINT
    show_low_conf_hint = bool(getattr(settings, "FOODAI_SHOW_LOW_CONF_HINT", False))
    if show_low_conf_hint:
        try:
            if float(conf) < conf_thr:
                parts.append(_("Внимание: низкая уверенность. Рекомендуем отредактировать перед сохранением."))
        except Exception:
            pass

    # Optional per-meal: absolute values + percent of daily plan (no day remainder here)
    if isinstance(itogo, dict):
        parts.append("")
        parts.append(_("📊 Итого:"))
        try:
            def _pct_show(x: float) -> str:
                try:
                    s = f"{float(x):.1f}"
                    if s.endswith(".0"):
                        return s[:-2]
                    return s
                except Exception:
                    try:
                        return str(int(round(float(x))))
                    except Exception:
                        return str(x)

            cal_pct = float(itogo.get("cal_pct") or 0)
            p_pct = float(itogo.get("p_pct") or 0)
            f_pct = float(itogo.get("f_pct") or 0)
            c_pct = float(itogo.get("c_pct") or 0)

            parts.append(f"🔥 Калории: {int(cal)} ккал ({_pct_show(cal_pct)}% от нормы)")
            parts.append(f"🥩 Белки: {float(p):.1f} г ({_pct_show(p_pct)}% от нормы)")
            parts.append(f"🥑 Жиры: {float(f):.1f} г ({_pct_show(f_pct)}% от нормы)")
            parts.append(f"🍞 Углеводы: {float(c):.1f} г ({_pct_show(c_pct)}% от нормы)")
        except Exception:
            pass

    return "\n".join(parts)


def _build_edit_prompt_text(
    cal: int,
    p: float,
    f: float,
    c: float,
    *,
    weight: float | None = None,
    items: list | None = None,
    title: str | None = None,
) -> str:
    parts: list[str] = []
    parts.append(_("✏️ Редактирование блюда"))
    parts.append("")
    if title:
        parts.append(f"<b>{_html_escape(str(title))}</b>")
    parts.append("")
    parts.append(_("🔥 Калории: {cal} ккал").format(cal=int(cal)))
    parts.append(_("🥩 Белки: {p} г").format(p=p))
    parts.append(_("🥑 Жиры: {f} г").format(f=f))
    parts.append(_("🍞 Углеводы: {c} г").format(c=c))
    if weight:
        parts.append(_("⚖️ Вес: {w} г").format(w=weight))
    if items:
        parts.append("")
        parts.append(_("🍜 Состав:"))
        for it in (items or []):
            try:
                name = str(it.get("name") or _("Блюдо"))
                w = it.get("weight_g")
                kc = it.get("calories")
                segs = []
                if w:
                    segs.append(f"{float(w):g} г")
                if kc is not None:
                    segs.append(f"{int(float(kc))} ккал")
                if segs:
                    parts.append(f"• {name} (" + ", ".join(segs) + ")")
                else:
                    parts.append(f"• {name}")
            except Exception:
                continue
    parts.append("")
    parts.append(_("📝 Что изменить в блюде? "))
    parts.append(_("Напишите только изменения, например: "))
    parts.append("• добавить кетчуп 10г")
    parts.append("• убрать соус")
    parts.append("• увеличить порцию в 2 раза")
    parts.append("• заменить рыбу на индейку")
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


def _edit_text_kb(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_("◀️ Назад"), callback_data=f"foodai:back:{meal_id}")],
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


def _saved_with_recommend_kb(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("🎲 Рекомендуй следующее блюдо"), callback_data=f"rec:start:{meal_id}")]]
    )


async def _edit_caption_or_text(cb: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    # Try caption edit first (if message has a photo), then text edit; finally fallback to sending a new message
    try:
        await cb.message.edit_caption(caption=text, reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await cb.message.edit_text(text=text, reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await cb.message.answer(text, reply_markup=kb)
    except Exception:
        # last resort: ignore
        pass


@router.callback_query(F.data.regexp(r"^foodai:save:(\d+)$"))
async def cb_foodai_save(callback: types.CallbackQuery, state: FSMContext) -> None:
    # Extra safety: ignore during onboarding or any active FSM state
    try:
        cur = await state.get_state()
        if cur is not None:
            await callback.answer()
            return
    except Exception:
        pass
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

            def _fmt(delta: float, emoji: str, unit: str, label: str) -> str:
                if unit == "ккал":
                    show_val = f"{int(abs(delta))}"
                else:
                    show_val = f"{abs(delta):.1f}"
                if delta > 0:
                    return f"{emoji} {label}: {show_val} {unit} до нормы"
                if delta < 0:
                    return f"⚠️ {emoji} {label}: +{show_val} {unit} превышено"
                return f"{emoji} {label}: норма достигнута"

            lines = [
                _("Анализ дня:"),
                _fmt(diff_cal, "🔥", "ккал", "Калории"),
                _fmt(diff_p, "🥩", "г", "Белки"),
                _fmt(diff_f, "🥑", "г", "Жиры"),
                _fmt(diff_c, "🍞", "г", "Углеводы"),
            ]
            analysis_text = "\n".join(lines)
    except Exception as e:
        logger.warning("day_analysis_failed | user_id={} | err={}", user_id, e)

    combined_text = saved_line if not analysis_text else f"{saved_line}\n\n{analysis_text}"
    await _edit_caption_or_text(callback, combined_text, kb=_saved_with_recommend_kb(meal_id))
    await callback.answer()


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
    await callback.answer()


@router.callback_query(F.data.regexp(r"^foodai:edit:(\d+)$"))
async def cb_foodai_edit(callback: types.CallbackQuery, state: FSMContext) -> None:
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
        # Build edit prompt text instead of standard preview
        text = _build_edit_prompt_text(
            int(meal.calories or 0),
            float(meal.protein_g or 0),
            float(meal.fat_g or 0),
            float(meal.carbs_g or 0),
            weight=float(meal.weight_g or 0),
            items=items,
            title=meal.title or None,
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

    # Enter edit FSM state and remember meal_id
    try:
        await state.set_state(EditStates.waiting_text)
        await state.update_data(edit_meal_id=meal_id)
    except Exception:
        pass

    await _edit_caption_or_text(callback, text, kb=_edit_text_kb(meal_id))
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
