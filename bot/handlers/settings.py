from __future__ import annotations

from aiogram import Router, types, F
from aiogram.utils.i18n import gettext as _
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import asyncio
import re

from bot.database.database import sessionmaker
from bot.services.templates import list_categories_with_counts
from bot.keyboards.templates import categories_browse_kb
from bot.services.account import get_account_summary_text
from bot.services.analytics import analytics
from bot.analytics.types import BaseEvent, EventProperties, Plan
from sqlalchemy import select
from bot.database.models import OnboardingAnswerModel
from bot.schemas.onboarding import DailyPlan, OnboardingData, Goal
from bot.services.adjust import parse_adjustment_cached, apply_adjustment, parse_adjustment_heuristic, rephrase_explanation_cached
from bot.core.config import settings
from bot.services.weight import get_current_weight
from loguru import logger
from bot.core.loader import redis_client
import hashlib

router = Router(name="settings")


class SettingsDailyNormStates(StatesGroup):
    waiting_text = State()


def _kb_settings() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton(text="📊 Суточная норма", callback_data="settings:open:daily_norm"),
        InlineKeyboardButton(text="📌 Шаблоны блюд", callback_data="settings:open:templates"),
    ])
    rows.append([
        InlineKeyboardButton(text="💎 Подписка", callback_data="settings:open:subscription"),
        InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:back:cabinet"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _templates_total_count(user_id: int) -> int:
    try:
        async with sessionmaker() as session:
            counts = await list_categories_with_counts(session, user_id)
        return int(sum(int(v or 0) for v in (counts or {}).values()))
    except Exception:
        return 0


async def _render_settings(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    n = await _templates_total_count(user_id)
    text = (
        "⚙️ Настройки\n\n"
        f"💎 Подписка: ❌ не активна\n"
        f"📝 Шаблонов блюд: {n}"
    )
    kb = _kb_settings()
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)

    # Analytics
    if analytics.logger:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Settings:Open",
                    event_properties=EventProperties(text="Settings:Open"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
        except Exception:
            pass


@router.callback_query(F.data == "settings:open")
async def cb_settings_open(callback: types.CallbackQuery) -> None:
    await _render_settings(callback)
    await callback.answer()


@router.callback_query(F.data == "settings:back:cabinet")
async def cb_settings_back_cabinet(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    text = await get_account_summary_text(user_id)
    # Reuse account keyboard if available (currently only weight button)
    from bot.handlers.account import _kb_account  # local import to avoid cycles at module load
    kb = _kb_account()
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    try:
        if analytics.logger:
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Settings:BackToCabinet",
                    event_properties=EventProperties(text="Settings:BackToCabinet"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "settings:open:daily_norm")
async def cb_settings_open_daily_norm(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    try:
        async with sessionmaker() as session:
            existing = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
            )
    except Exception as e:
        existing = None
        logger.warning("settings.daily_norm.load_error | user_id={} | err={}", user_id, e)

    if not existing:
        text = (
            "📊 Суточная норма\n\n"
            "Не нашёл базовые данные. Пройди короткую настройку: /start"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open")]]
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        await callback.answer()
        return

    dp_json = dict(existing.daily_plan or {})
    data_json = dict(existing.data or {})
    try:
        plan = DailyPlan.model_validate(dp_json)
    except Exception:
        plan = DailyPlan(calories=int(dp_json.get("calories") or 0), protein_g=int(dp_json.get("protein_g") or 0), fat_g=int(dp_json.get("fat_g") or 0), carbs_g=int(dp_json.get("carbs_g") or 0))

    goal_weight = data_json.get("goal_weight_kg")
    try:
        cw = await get_current_weight(user_id)
    except Exception:
        cw = None
    if cw is None:
        try:
            cw = float(data_json.get("weight_kg")) if data_json.get("weight_kg") is not None else None
        except Exception:
            cw = None
    remain_str = "нет данных"
    goal_str = "нет данных"
    try:
        if goal_weight is not None:
            goal_str = f"{float(goal_weight)} кг"
        if cw is not None and goal_weight is not None:
            remain = abs(float(cw) - float(goal_weight))
            remain_str = f"{round(remain, 1)} кг"
    except Exception:
        pass

    text = (
        "📊 Суточная норма\n\n"
        f"🔥 Калории: {int(getattr(plan, 'calories', 0) or 0)} ккал\n"
        f"🥩 Белки: {int(getattr(plan, 'protein_g', 0) or 0)} г\n"
        f"🥑 Жиры: {int(getattr(plan, 'fat_g', 0) or 0)} г\n"
        f"🍞 Углеводы: {int(getattr(plan, 'carbs_g', 0) or 0)} г\n\n"
        f"🎯 Цель: {goal_str} | До цели: {remain_str}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить план питания", callback_data="daily_norm:edit_start"), InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    try:
        if analytics.logger:
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Settings:DailyNormOpen",
                    event_properties=EventProperties(text="Settings:DailyNormOpen"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "daily_norm:edit_start")
async def cb_daily_norm_edit_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return
    await state.set_state(SettingsDailyNormStates.waiting_text)
    text = "Напиши, в свободном формате, что нужно скорректировать в твоём индивидуальном плане"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:daily_norm")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    try:
        if analytics.logger and callback.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="Settings:DailyNormEditStart",
                    event_properties=EventProperties(text="Settings:DailyNormEditStart"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()


@router.message(SettingsDailyNormStates.waiting_text, F.text & (~F.text.startswith("/")))
async def daily_norm_adjust_apply(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    text_raw = (message.text or "").strip()
    try:
        await message.answer(_("✨ Изучаю ваши пожелания и обновляю план..."))
    except Exception:
        pass

    try:
        async with sessionmaker() as session:
            existing = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
            )
            if not existing:
                await message.answer(_("Не нашёл базовые данные. Пройди короткую настройку: /start"))
                await state.clear()
                return

            data_json = dict(existing.data or {})
            dp_json = dict(existing.daily_plan or {})
            try:
                base_plan_dict = data_json.get("base_plan") or dp_json
                base_plan = DailyPlan.model_validate(base_plan_dict)
            except Exception:
                base_plan = DailyPlan.model_validate(dp_json)
                data_json["base_plan"] = base_plan.model_dump(mode="json")
            try:
                payload = OnboardingData.model_validate(data_json)
            except Exception as e:
                logger.warning("settings.daily_norm.payload_invalid | user_id={} | err={}", user_id, e)
                await message.answer(_("Данные повреждены. Попробуй заново: /start"))
                await state.clear()
                return

            try:
                plan_key_str = f"{int(base_plan.calories)}:{int(base_plan.protein_g)}:{int(base_plan.fat_g)}:{int(base_plan.carbs_g)}"
            except Exception:
                plan_key_str = "0:0:0:0"
            try:
                plan_key = hashlib.sha256(plan_key_str.encode("utf-8")).hexdigest()[:16]
            except Exception:
                plan_key = None
            try:
                base_ctx = {
                    "base_plan": {
                        "calories": int(base_plan.calories),
                        "protein_g": int(base_plan.protein_g),
                        "fat_g": int(base_plan.fat_g),
                        "carbs_g": int(base_plan.carbs_g),
                    },
                    "goal": payload.goal.value if hasattr(payload.goal, "value") else str(payload.goal),
                    "weight_kg": float(payload.weight_kg),
                    "goal_weight_kg": float(payload.goal_weight_kg) if payload.goal_weight_kg is not None else None,
                    "activity_level": payload.activity_level.value if hasattr(payload.activity_level, "value") else str(payload.activity_level),
                }
            except Exception:
                base_ctx = None

            parsed = await parse_adjustment_cached(
                user_id,
                text_raw,
                lang_hint=getattr(message.from_user, "language_code", None),
                plan_key=plan_key,
                base_ctx=base_ctx,
            )
            if not parsed:
                if str(getattr(settings, "ADJUST_ENGINE_MODE", "")).lower() == "llm_only":
                    await message.answer(_("Не до конца понял запрос. Сформулируй одной фразой, например: \n• 'уменьши углеводы на 10%' \n• 'хочу быстрее похудеть' \n• 'к 01.03.2026' \n• 'мало двигаюсь — поставь низкую активность'"))
                    return
                h = parse_adjustment_heuristic(text_raw)
                if h:
                    parsed = h
                else:
                    await message.answer(_("Не до конца понял запрос. Сформулируй одной фразой, например: \n• 'уберите углеводы' \n• 'добавь 200 ккал' \n• 'мало двигаюсь — поставь низкую активность'"))
                    return

            new_plan, explanation, summary = apply_adjustment(base_plan, payload, parsed)

            existing.daily_plan = new_plan.model_dump(mode="json")
            try:
                existing.goal = payload.goal.value if hasattr(payload.goal, "value") else str(payload.goal)
            except Exception:
                pass
            try:
                existing.calories = int(new_plan.calories)
            except Exception:
                pass
            await session.commit()
            # Invalidate cached account summary to reflect new plan immediately
            try:
                await redis_client.delete(f"account:summary:{user_id}")
            except Exception:
                pass

    except Exception as e:
        logger.exception("settings.daily_norm.apply_error | user_id={} | err={}", user_id, e)
        await message.answer(_("Не удалось сохранить изменения. Попробуй позже."))
        await state.clear()
        return

    personal_line: str | None = None
    try:
        note = getattr(parsed, 'rationale', None)
        intents = list(getattr(parsed, 'intents', []) or [])
        if isinstance(note, str) and note.strip():
            personal_line = "Учёл запрос: " + note.strip()
        else:
            intent_map = {
                'low_fodmap_candidate': "уменьшить FODMAP-продукты",
                'lactose_free': "избегать лактозы",
                'gluten_free': "без глютена",
                'sugar_free': "ограничить сахар",
                'keto': "кето-схему",
                'low_carb': "снизить углеводы",
                'high_protein': "акцент на белок",
                'raise_calories': "увеличить калорийность",
                'lower_calories': "снизить калорийность",
                'activity_down': "понизить активность",
                'activity_up': "повысить активность",
                'reduce_protein': "снизить белок",
                'reduce_fat': "снизить жиры",
                'increase_fat': "повысить жиры",
                'custom_macros': "кастомные макросы",
            }
            phrases = [intent_map[i] for i in intents if i in intent_map]
            if phrases:
                personal_line = "Учёл запрос: " + ", ".join(phrases)
    except Exception:
        personal_line = None

    lines: list[str] = []
    lines.append("<b>Твой план скорректирован!</b>")
    lines.append("")
    try:
        if payload.goal != Goal.maintain:
            if getattr(new_plan, 'eta_date', None) is not None and getattr(payload, 'goal_weight_kg', None) is not None:
                delta = abs(float(payload.weight_kg) - float(payload.goal_weight_kg))
                formatted_date = new_plan.eta_date.strftime('%d.%m.%Y')
                if payload.goal == Goal.lose:
                    lines.append(f"Ты сбросишь {round(delta, 1)} кг к {formatted_date}")
                elif payload.goal == Goal.gain:
                    lines.append(f"Ты наберешь {round(delta, 1)} кг к {formatted_date}")
            lines.append(f"Скорость: {getattr(new_plan, 'weekly_rate_kg', 0)} кг в неделю")
    except Exception:
        pass
    lines.append("")
    lines.append("<b>Обновленная дневная норма:</b>")
    lines.append(f"🔥 Калории: {new_plan.calories} ккал")
    lines.append(f"🥩 Белки: {new_plan.protein_g} г")
    lines.append(f"🥑 Жиры: {new_plan.fat_g} г")
    lines.append(f"🍞 Углеводы: {new_plan.carbs_g} г")
    lines.append("")
    if personal_line:
        def _norm_txt(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").lower()).strip()
        pl_core = re.sub(r"^уч[её]л\s+запрос:\s*", "", personal_line, flags=re.I)
        if _norm_txt(pl_core) and _norm_txt(pl_core) not in _norm_txt(explanation):
            lines.append(personal_line)
    lines.append(explanation)
    lines.append("")
    lines.append("Оставим так или нужна еще корректировка?")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отлично", callback_data="daily_norm:final:ok")],
            [InlineKeyboardButton(text="Хочу скорректировать", callback_data="daily_norm:final:adjust")],
        ]
    )
    sent_msg = await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
    try:
        should_try_rephrase = (
            settings.ADJUST_REPHRASE_ENABLED
            and explanation
            and len(explanation) >= int(getattr(settings, "ADJUST_REPHRASE_LENGTH_MIN", 220) or 220)
        )
        if should_try_rephrase:
            async def _rephrase_and_edit() -> None:
                try:
                    rewritten = await rephrase_explanation_cached(explanation, settings.ADJUST_REPHRASE_TONE or "neutral")
                    if not rewritten:
                        return
                    new_lines: list[str] = []
                    new_lines.append("<b>Твой план скорректирован!</b>")
                    new_lines.append("")
                    try:
                        if payload.goal != Goal.maintain:
                            if getattr(new_plan, 'eta_date', None) is not None and getattr(payload, 'goal_weight_kg', None) is not None:
                                delta2 = abs(float(payload.weight_kg) - float(payload.goal_weight_kg))
                                formatted_date2 = new_plan.eta_date.strftime('%d.%m.%Y')
                                if payload.goal == Goal.lose:
                                    new_lines.append(f"Ты сбросишь {round(delta2, 1)} кг к {formatted_date2}")
                                elif payload.goal == Goal.gain:
                                    new_lines.append(f"Ты наберешь {round(delta2, 1)} кг к {formatted_date2}")
                            new_lines.append(f"Скорость: {getattr(new_plan, 'weekly_rate_kg', 0)} кг в неделю")
                    except Exception:
                        pass
                    new_lines.append("")
                    new_lines.append("<b>Обновленная дневная норма:</b>")
                    new_lines.append(f"🔥 Калории: {new_plan.calories} ккал")
                    new_lines.append(f"🥩 Белки: {new_plan.protein_g} г")
                    new_lines.append(f"🥑 Жиры: {new_plan.fat_g} г")
                    new_lines.append(f"🍞 Углеводы: {new_plan.carbs_g} г")
                    new_lines.append("")
                    if personal_line:
                        def _norm_txt2(s: str) -> str:
                            return re.sub(r"\s+", " ", (s or "").lower()).strip()
                        pl_core2 = re.sub(r"^уч[её]л\s+запрос:\s*", "", personal_line, flags=re.I)
                        if _norm_txt2(pl_core2) and _norm_txt2(pl_core2) not in _norm_txt2(rewritten):
                            new_lines.append(personal_line)
                    new_lines.append(rewritten)
                    new_lines.append("")
                    new_lines.append("Оставим так или нужна еще корректировка?")
                    try:
                        await sent_msg.edit_text("\n".join(new_lines), reply_markup=kb)
                    except Exception:
                        pass
                except Exception:
                    pass
            asyncio.create_task(_rephrase_and_edit())
    except Exception:
        pass
    try:
        if analytics.logger:
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Settings:DailyNormAdjustApplied",
                    event_properties=EventProperties(text="Settings:DailyNormAdjustApplied"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await state.clear()


@router.callback_query(F.data == "daily_norm:final:ok")
async def cb_daily_norm_final_ok(callback: types.CallbackQuery) -> None:
    if callback.from_user:
        try:
            if analytics.logger:
                analytics.fire_event(
                    BaseEvent(
                        user_id=callback.from_user.id,
                        event_type="Settings:DailyNormFinalOk",
                        event_properties=EventProperties(text="Settings:DailyNormFinalOk"),
                        plan=Plan(branch="Settings", source="Bot", version="v1"),
                    )
                )
        except Exception:
            pass
    await cb_settings_open(callback)


@router.callback_query(F.data == "daily_norm:final:adjust")
async def cb_daily_norm_final_adjust(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsDailyNormStates.waiting_text)
    text = "Напиши, в свободном формате, что нужно скорректировать в твоём индивидуальном плане"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:daily_norm")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    try:
        if analytics.logger and callback.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="Settings:DailyNormFinalAdjust",
                    event_properties=EventProperties(text="Settings:DailyNormFinalAdjust"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "settings:open:subscription")
async def cb_settings_open_subscription(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    text = (
        "💎 Подписка\n\n"
        "Функционал подписки будет доступен позже."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    try:
        if analytics.logger and callback.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="Settings:ClickSubscription",
                    event_properties=EventProperties(text="Settings:ClickSubscription"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()


def _templates_root_with_back_kb(counts: dict[str, int] | None) -> InlineKeyboardMarkup:
    base = categories_browse_kb(counts)
    rows = [list(row) for row in base.inline_keyboard]
    rows.append([InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="templates:back:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "settings:open:templates")
async def cb_settings_open_templates(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        counts = await list_categories_with_counts(session, user_id)
    text = _("Выбери категорию приёма пищи:")
    kb = _templates_root_with_back_kb(counts)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    try:
        if analytics.logger:
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Settings:ClickTemplates",
                    event_properties=EventProperties(text="Settings:ClickTemplates"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "templates:back:settings")
async def cb_templates_back_settings(callback: types.CallbackQuery) -> None:
    await _render_settings(callback)
    try:
        if analytics.logger and callback.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="Templates:BackToSettings",
                    event_properties=EventProperties(text="Templates:BackToSettings"),
                    plan=Plan(branch="Templates", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()
