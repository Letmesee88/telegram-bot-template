from __future__ import annotations
import asyncio
import contextlib
import hashlib
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import F, Router, types
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select, update

from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.core.config import settings
from bot.core.loader import redis_client
from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, SubscriptionModel, UserModel
from bot.keyboards.templates import categories_browse_kb
from bot.schemas.onboarding import DailyPlan, Goal, OnboardingData
from bot.services.account import get_account_summary_text
from bot.services.adjust import (
    apply_adjustment,
    parse_adjustment_cached,
    parse_adjustment_heuristic,
    rephrase_explanation_cached,
)
from bot.services.analytics import analytics
from bot.services.plan import calculate_daily_plan
from bot.services.subscriptions import activate_free_trial, is_free_trial_available, trial_days
from bot.services.templates import list_categories_with_counts
from bot.services.users import get_user_tzinfo
from bot.services.weight import get_current_weight

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext

router = Router(name="settings")


class SettingsDailyNormStates(StatesGroup):
    waiting_text = State()


class SubscriptionEmailStates(StatesGroup):
    waiting_email = State()


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")


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
    # Build subscription topline (Variant A)
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        tzinfo = await get_user_tzinfo(session, user_id)
    def _fmt(dt):
        try:
            return dt.astimezone(tzinfo).strftime("%d.%m.%Y") if dt else "—"
        except Exception:
            return "—"
    plan_map_short = {"trial": "пробный", "month": "месяц", "year": "год"}
    now_utc = datetime.now(timezone.utc)
    sub_is_active_now = bool(
        sub and sub.status == "active" and sub.expires_at_utc and sub.expires_at_utc > now_utc
    )
    if sub_is_active_now:
        line_sub = f"Подписка: {plan_map_short.get(sub.plan, sub.plan)} - до {_fmt(sub.expires_at_utc)}"
    else:
        line_sub = "Подписка: не активна"
    text = (
        "⚙️ Настройки\n\n"
        f"{line_sub}\n"
        f"📝 Шаблонов блюд: {n}"
    )
    kb = _kb_settings()
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)

    # Analytics
    if analytics.logger:
        with contextlib.suppress(Exception):
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Settings:Open",
                    event_properties=EventProperties(text="Settings:Open"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )


@router.callback_query(F.data == "settings:open")
async def cb_settings_open(callback: types.CallbackQuery) -> None:
    await _render_settings(callback)
    await callback.answer()


@router.callback_query(F.data == "subscription:change_plan")
async def cb_subscription_change_plan(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        tzinfo = await get_user_tzinfo(session, user_id)
    if not sub or not sub.expires_at_utc:
        await cb_settings_open_subscription(callback)
        return
    plan_map = {"trial": "Пробный доступ", "month": "Месячная подписка", "year": "Годовая подписка"}
    base_plan = getattr(sub, "next_plan", None)
    base_plan = base_plan if base_plan in {"month", "year"} else sub.plan
    target = "year" if base_plan == "month" else "month"
    title = "🔄 Смена плана подписки"
    datetime.now(timezone.utc)
    try:
        cur_until = sub.expires_at_utc.astimezone(tzinfo).strftime("%d.%m.%Y")
    except Exception:
        cur_until = "—"
    lines = [
        title,
        "",
        f"Текущий план: {plan_map.get(sub.plan, sub.plan)}",
        f"📅 Действует до: {cur_until}",
        "",
    ]
    if target == "year":
        lines += [
            "Предлагаемый план: Годовая подписка",
            "",
            "💰 Стоимость: 2500 руб/год",
            "",
            "При смене на годовой план:",
            "• Вы сэкономите 6 500 рублей в год",
            "• Не нужно беспокоиться о ежемесячных платежах",
            "• Все функции остаются доступными",
        ]
        cta = "💎 Перейти на годовую подписку"
    else:
        lines += [
            "Предлагаемый план: Месячная подписка",
            "",
            "💰 Стоимость: 750 руб/месяц",
        ]
        cta = "💎 Перейти на месячную подписку"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=cta, callback_data=f"subscription:change_plan_confirm:{target}")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:subscription")],
    ])
    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "subscription:autorenew:disable")
async def cb_subscription_autorenew_disable(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        if not sub or not sub.expires_at_utc:
            await callback.answer()
            return
        await session.execute(update(SubscriptionModel).where(SubscriptionModel.id == sub.id).values(auto_renew=False))
        await session.commit()
        tzinfo = await get_user_tzinfo(session, user_id)
        until = sub.expires_at_utc.astimezone(tzinfo)
    now_utc = datetime.now(timezone.utc).astimezone(tzinfo)
    days_left = max(0, (until.date() - now_utc.date()).days)
    lines = [
        "✅ Автопродление отключено",
        "",
        f"Подписка остается активной до {until.strftime('%d.%m.%Y')}",
        f"Осталось дней: {days_left}",
        "",
        "Автоматическое продление больше не будет происходить.",
        "Вы можете возобновить подписку в любое время.",
        "",
        "Спасибо за использование Calorissimo ai! 🍬",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↔️ Сменить план", callback_data="subscription:change_plan")],
        [InlineKeyboardButton(text="✅ Включить автопродление", callback_data="subscription:autorenew:enable")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:subscription")],
    ])
    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "subscription:autorenew:enable")
async def cb_subscription_autorenew_enable(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        if not sub or not sub.expires_at_utc:
            await callback.answer()
            return
        await session.execute(update(SubscriptionModel).where(SubscriptionModel.id == sub.id).values(auto_renew=True))
        await session.commit()
        tzinfo = await get_user_tzinfo(session, user_id)
        until = sub.expires_at_utc.astimezone(tzinfo)
    now_utc = datetime.now(timezone.utc).astimezone(tzinfo)
    days_left = max(0, (until.date() - now_utc.date()).days)
    lines = [
        "✅ Автопродление включено",
        "",
        f"Подписка будет автоматически продлена {until.strftime('%d.%m.%Y')}",
        f"Осталось дней: {days_left}",
        "",
        "Спасибо, что остаетесь с нами! 🎉",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↔️ Сменить план", callback_data="subscription:change_plan")],
        [InlineKeyboardButton(text="❌ Отключить подписку", callback_data="subscription:autorenew:disable")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:subscription")],
    ])
    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data.startswith("subscription:change_plan_confirm:"))
async def cb_subscription_change_plan_confirm(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    target = (callback.data or "").split(":")[-1]
    if target not in {"month", "year"}:
        await callback.answer()
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        tzinfo = await get_user_tzinfo(session, user_id)
    if not sub or not sub.expires_at_utc:
        await cb_settings_open_subscription(callback)
        return
    plan_map = {"trial": "Пробный доступ", "month": "Месячная подписка", "year": "Годовая подписка"}
    cur_plan = plan_map.get(sub.plan, sub.plan)
    new_plan = plan_map.get(target, target)
    until = sub.expires_at_utc.astimezone(tzinfo).strftime("%d.%m.%Y")
    lines = [
        "✅ Подтверждение смены плана",
        "",
        f"Сейчас: {cur_plan}",
        f"На: {new_plan}",
        "",
        "📅 Когда произойдет смена:",
        f"В конце текущего периода ({until})",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить смену", callback_data=f"subscription:change_plan_apply:{target}")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="subscription:change_plan")],
    ])
    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data.startswith("subscription:change_plan_apply:"))
async def cb_subscription_change_plan_apply(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    target = (callback.data or "").split(":")[-1]
    if target not in {"month", "year"}:
        await callback.answer()
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        tzinfo = await get_user_tzinfo(session, user_id)
        if not sub:
            await callback.answer()
            return
        await session.execute(
            update(SubscriptionModel).where(SubscriptionModel.id == sub.id).values(next_plan=target)
        )
        await session.commit()
    plan_map = {"trial": "Пробный доступ", "month": "Месячная подписка", "year": "Годовая подписка"}
    new_plan = plan_map.get(target, target)
    until = "—"
    try:
        async with sessionmaker() as session:
            sub2 = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
            if sub2 and sub2.expires_at_utc:
                tzinfo = await get_user_tzinfo(session, user_id)
                until = sub2.expires_at_utc.astimezone(tzinfo).strftime("%d.%m.%Y")
    except Exception:
        pass
    lines = [
        "🎉 План успешно изменен!",
        "",
        f"Новый план: {new_plan}",
        f"📅 Дата списания: {until}",
        "✅ Смена плана завершена!",
        "",
        "Следующий платеж будет по новому тарифу.",
    ]
    # Actions after success
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
    kb_rows = [[InlineKeyboardButton(text="↔️ Сменить план", callback_data="subscription:change_plan")]]
    if sub and bool(getattr(sub, "auto_renew", True)):
        kb_rows.append([InlineKeyboardButton(text="❌ Отключить подписку", callback_data="subscription:autorenew:disable")])
    else:
        kb_rows.append([InlineKeyboardButton(text="✅ Включить автопродление", callback_data="subscription:autorenew:enable")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:subscription")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
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
async def cb_settings_open_daily_norm(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return
    try:
        cur = await state.get_state()
        if cur == SettingsDailyNormStates.waiting_text.state:
            await state.clear()
    except Exception:
        pass
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
    # Safely extract displayed values even if daily_plan is partial
    cal_val = None
    p_val = None
    f_val = None
    c_val = None
    try:
        plan_obj = DailyPlan.model_validate(dp_json)
        cal_val = int(getattr(plan_obj, "calories", 0) or 0)
        p_val = int(getattr(plan_obj, "protein_g", 0) or 0)
        f_val = int(getattr(plan_obj, "fat_g", 0) or 0)
        c_val = int(getattr(plan_obj, "carbs_g", 0) or 0)
    except Exception:
        try:
            cal_val = int(dp_json.get("calories") or 0)
            p_val = int(dp_json.get("protein_g") or 0)
            f_val = int(dp_json.get("fat_g") or 0)
            c_val = int(dp_json.get("carbs_g") or 0)
        except Exception:
            cal_val = p_val = f_val = c_val = 0

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
        f"🔥 Калории: {cal_val} ккал\n"
        f"🥩 Белки: {p_val} г\n"
        f"🥑 Жиры: {f_val} г\n"
        f"🍞 Углеводы: {c_val} г\n\n"
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
    with contextlib.suppress(Exception):
        await message.answer(_("✨ Изучаю ваши пожелания и обновляю план..."))

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

            # 2) Validate payload first
            try:
                payload = OnboardingData.model_validate(data_json)
            except Exception as e:
                logger.warning("settings.daily_norm.payload_invalid | user_id={} | err={}", user_id, e)
                await message.answer(_("Данные повреждены. Попробуй заново: /start"))
                await state.clear()
                return

            # 3) Build base_plan with robust fallbacks
            try:
                base_plan_dict = data_json.get("base_plan") or dp_json
                base_plan = DailyPlan.model_validate(base_plan_dict)
            except Exception:
                try:
                    base_plan = DailyPlan.model_validate(dp_json)
                    data_json["base_plan"] = base_plan.model_dump(mode="json")
                except Exception:
                    # Compute from payload to ensure full fields present
                    try:
                        calc = calculate_daily_plan(payload)
                        base_plan = calc
                        data_json["base_plan"] = {
                            "calories": int(getattr(calc, "calories", 0) or 0),
                            "protein_g": int(getattr(calc, "protein_g", 0) or 0),
                            "fat_g": int(getattr(calc, "fat_g", 0) or 0),
                            "carbs_g": int(getattr(calc, "carbs_g", 0) or 0),
                            "sources": list(getattr(calc, "sources", []) or []),
                            "tdee": int(getattr(calc, "tdee", 0) or 0),
                            "weekly_rate_kg": float(getattr(calc, "weekly_rate_kg", 0.0) or 0.0),
                            "eta_date": getattr(calc, "eta_date", None),
                        }
                    except Exception:
                        # Last resort: use calculate_daily_plan even if data_json is imperfect
                        calc = calculate_daily_plan(payload)
                        base_plan = calc

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
            with contextlib.suppress(Exception):
                existing.goal = payload.goal.value if hasattr(payload.goal, "value") else str(payload.goal)
            with contextlib.suppress(Exception):
                existing.calories = int(new_plan.calories)
            await session.commit()
            # Invalidate cached account summary to reflect new plan immediately
            with contextlib.suppress(Exception):
                await redis_client.delete(f"account:summary:{user_id}")

    except Exception as e:
        logger.exception("settings.daily_norm.apply_error | user_id={} | err={}", user_id, e)
        await message.answer(_("Не удалось сохранить изменения. Попробуй позже."))
        await state.clear()
        return

    personal_line: str | None = None
    try:
        note = getattr(parsed, "rationale", None)
        intents = list(getattr(parsed, "intents", []) or [])
        if isinstance(note, str) and note.strip():
            personal_line = "Учёл запрос: " + note.strip()
        else:
            intent_map = {
                "low_fodmap_candidate": "уменьшить FODMAP-продукты",
                "lactose_free": "избегать лактозы",
                "gluten_free": "без глютена",
                "sugar_free": "ограничить сахар",
                "keto": "кето-схему",
                "low_carb": "снизить углеводы",
                "high_protein": "акцент на белок",
                "raise_calories": "увеличить калорийность",
                "lower_calories": "снизить калорийность",
                "activity_down": "понизить активность",
                "activity_up": "повысить активность",
                "reduce_protein": "снизить белок",
                "reduce_fat": "снизить жиры",
                "increase_fat": "повысить жиры",
                "custom_macros": "кастомные макросы",
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
            if getattr(new_plan, "eta_date", None) is not None and getattr(payload, "goal_weight_kg", None) is not None:
                delta = abs(float(payload.weight_kg) - float(payload.goal_weight_kg))
                formatted_date = new_plan.eta_date.strftime("%d.%m.%Y")
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
        pl_core = re.sub(r"^уч[её]л\s+запрос:\s*", "", personal_line, flags=re.IGNORECASE)
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
                            if getattr(new_plan, "eta_date", None) is not None and getattr(payload, "goal_weight_kg", None) is not None:
                                delta2 = abs(float(payload.weight_kg) - float(payload.goal_weight_kg))
                                formatted_date2 = new_plan.eta_date.strftime("%d.%m.%Y")
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
                        pl_core2 = re.sub(r"^уч[её]л\s+запрос:\s*", "", personal_line, flags=re.IGNORECASE)
                        if _norm_txt2(pl_core2) and _norm_txt2(pl_core2) not in _norm_txt2(rewritten):
                            new_lines.append(personal_line)
                    new_lines.append(rewritten)
                    new_lines.append("")
                    new_lines.append("Оставим так или нужна еще корректировка?")
                    with contextlib.suppress(Exception):
                        await sent_msg.edit_text("\n".join(new_lines), reply_markup=kb)
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
    user_id = callback.from_user.id
    now_utc = datetime.now(timezone.utc)

    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        tzinfo = await get_user_tzinfo(session, user_id)

    def fmt(dt):
        try:
            return dt.astimezone(tzinfo).strftime("%d.%m.%Y %H:%M") if dt else "?"
        except Exception:
            return "?"

    lines: list[str] = []
    kb_rows: list[list[InlineKeyboardButton]] = []
    sub_is_active_now = bool(
        sub and sub.status == "active" and sub.expires_at_utc and sub.expires_at_utc > now_utc
    )

    if sub_is_active_now:
        plan_map = {"trial": "пробный", "month": "месяц", "year": "год"}
        status_text = "пробный период" if sub.plan == "trial" else "активна"
        try:
            days_left = max(0, (sub.expires_at_utc.astimezone(tzinfo).date() - now_utc.astimezone(tzinfo).date()).days)
        except Exception:
            days_left = 0

        lines.append("Управление подпиской")
        lines.append("")
        lines.append(f"Статус: {status_text}")
        lines.append(f"Тариф: {plan_map.get(sub.plan, sub.plan)}")
        lines.append(f"Действует до: {fmt(sub.expires_at_utc)}")
        lines.append(f"Автопродление: {'да' if bool(getattr(sub, 'auto_renew', True)) else 'нет'}")
        lines.append(f"Дней осталось: {days_left}")
        lines.append("")

        kb_rows.append([InlineKeyboardButton(text="Сменить тариф", callback_data="subscription:change_plan")])
        if bool(getattr(sub, "auto_renew", True)):
            kb_rows.append([InlineKeyboardButton(text="Отключить автопродление", callback_data="subscription:autorenew:disable")])
        else:
            kb_rows.append([InlineKeyboardButton(text="Включить автопродление", callback_data="subscription:autorenew:enable")])
        kb_rows.append([InlineKeyboardButton(text="Назад", callback_data="settings:open")])
    else:
        lines.append("💎 Управление подпиской")
        lines.append("")
        lines.append("У тебя нет активной подписки.")
        lines.append("")
        lines.append("Выбери вариант:")

        trial_available = False
        try:
            async with sessionmaker() as session:
                trial_available = await is_free_trial_available(session, user_id)
        except Exception as e:
            logger.warning("settings.subscription.trial_check_failed | user_id={} | err={}", user_id, e)

        if trial_available:
            kb_rows.append([InlineKeyboardButton(text="3 дня бесплатно", callback_data="subscription:buy:trial")])
        kb_rows.append([InlineKeyboardButton(text="750 руб / месяц", callback_data="subscription:buy:month")])
        kb_rows.append([InlineKeyboardButton(text="2500 руб / год", callback_data="subscription:buy:year")])
        kb_rows.append([InlineKeyboardButton(text="Назад", callback_data="settings:open")])

    text_out = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await callback.message.edit_text(text_out, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text_out, reply_markup=kb, disable_web_page_preview=True)

    try:
        if analytics.logger and callback.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Settings:ClickSubscription",
                    event_properties=EventProperties(text="Settings:ClickSubscription"),
                    plan=Plan(branch="Settings", source="Bot", version="v1"),
                )
            )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "subscription:auto_renew:toggle")
async def cb_subscription_toggle_autorenew(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        if sub is None:
            await callback.answer()
            return
        new_val = not bool(getattr(sub, "auto_renew", True))
        await session.execute(
            update(SubscriptionModel).where(SubscriptionModel.id == sub.id).values(auto_renew=new_val)
        )
        await session.commit()
    await cb_settings_open_subscription(callback)


@router.callback_query(F.data.startswith("subscription:set_next:"))
async def cb_subscription_set_next(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    plan = (callback.data or "").split(":")[-1]
    if plan not in {"month", "year"}:
        await callback.answer()
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        if sub is None:
            await callback.answer()
            return
        await session.execute(
            update(SubscriptionModel).where(SubscriptionModel.id == sub.id).values(next_plan=plan)
        )
        await session.commit()
    await cb_settings_open_subscription(callback)


@router.callback_query(F.data == "subscription:buy:trial")
async def cb_subscription_buy_trial(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id

    trial_available = False
    try:
        async with sessionmaker() as session:
            trial_available = await is_free_trial_available(session, user_id)
    except Exception as e:
        logger.warning("settings.buy_trial.check_failed | user_id={} | err={}", user_id, e)

    if not trial_available:
        await callback.message.answer("Бесплатный пробный период уже использован. Можно перейти на платный тариф.")
        await cb_settings_open_subscription(callback)
        await callback.answer()
        return

    try:
        async with sessionmaker() as session:
            tz = await get_user_tzinfo(session, user_id)
    except Exception:
        tz = timezone.utc

    days = trial_days()
    end_dt = (datetime.now(tz) + timedelta(days=days)).strftime("%d.%m.%Y %H:%M")
    text_out = (
        "Бесплатный пробный период\n\n"
        f"Длительность: {days} дн.\n"
        "Стоимость: 0 руб\n\n"
        f"Действует до: {end_dt}\n\n"
        "После окончания пробного периода выберите платный тариф."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Активировать бесплатно", callback_data="subscription:start:trial")],
        [InlineKeyboardButton(text="Назад", callback_data="settings:open:subscription")],
    ])
    try:
        await callback.message.edit_text(text_out, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text_out, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "subscription:buy:month")
async def cb_subscription_buy_month(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    text_out = (
        "💎 Оплата подписки\n\n"
        "План: Месячная подписка\n"
        "Стоимость: 750 руб/месяц\n"
        "Период: 30 дней\n\n"
        "После оплаты подписка будет автоматически продлеваться.\n\n"
        "Оплачивая, ты соглашаешься с <a href=\"https://telegra.ph/Polzovatelskoe-soglashenie-12-05-32\">Пользовательским соглашением</a>, "
        "<a href=\"https://telegra.ph/Politika-konfidencialnosti-12-05-33\">Политикой конфиденциальности</a> и на сохранение способа оплаты для автопродления.\n"
        "Автосписание можно отключить в разделе «Настройки → Подписка»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 750 руб", callback_data="subscription:pay:month")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:subscription")],
    ])
    try:
        await callback.message.edit_text(text_out, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text_out, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "subscription:buy:year")
async def cb_subscription_buy_year(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    text_out = (
        "💎 Оплата подписки\n\n"
        "План: Годовая подписка\n"
        "Стоимость: 2500 руб/в год\n"
        "Период: 365 дней\n\n"
        "После оплаты подписка будет автоматически продлеваться.\n\n"
        "Оплачивая, ты соглашаешься с <a href=\"https://telegra.ph/Polzovatelskoe-soglashenie-12-05-32\">Пользовательским соглашением</a>, "
        "<a href=\"https://telegra.ph/Politika-konfidencialnosti-12-05-33\">Политикой конфиденциальности</a> и на сохранение способа оплаты для автопродления.\n"
        "Автосписание можно отключить в разделе «Настройки → Подписка»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 2500 руб", callback_data="subscription:pay:year")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="settings:open:subscription")],
    ])
    try:
        await callback.message.edit_text(text_out, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text_out, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


async def _check_email_and_pay(callback: types.CallbackQuery, state: FSMContext, plan: str) -> None:
    """Check if user has email, if not ask for it, otherwise create payment."""
    if not callback.from_user:
        return
    if plan not in {"month", "year"}:
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    user_id = callback.from_user.id
    async with sessionmaker() as session:
        user_email = await session.scalar(select(UserModel.email).where(UserModel.id == user_id))

    if not user_email:
        await state.set_state(SubscriptionEmailStates.waiting_email)
        await state.update_data(pending_plan=plan)
        text_out = "Нужен e-mail для чека. Введите, пожалуйста, в формате: yourmail@example.ru"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="subscription:email:cancel")],
        ])
        await callback.message.answer(text_out, reply_markup=kb)
        await callback.answer()
        return

    await _create_and_show_payment(callback, user_id, plan)


async def _create_and_show_payment(callback: types.CallbackQuery, user_id: int, plan: str) -> None:
    """Create payment and show payment link."""
    from bot.services.yookassa import create_payment

    plan_info = {
        "month": ("750 руб", "750"),
        "year": ("2500 руб", "2500"),
    }
    btn_text, _ = plan_info.get(plan, ("тариф", "0"))

    try:
        cp = await create_payment(user_id=user_id, plan=plan)
    except Exception as e:
        logger.error(f"Payment creation failed: {e}")
        await callback.message.answer("Не удалось создать платеж. Попробуйте позже.")
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Оплатить {btn_text}", url=cp.confirmation_url)],
        [InlineKeyboardButton(text="Назад", callback_data="settings:open:subscription")],
    ])
    await callback.message.answer("Перейдите по ссылке для оплаты:", reply_markup=kb, disable_web_page_preview=True)
    with contextlib.suppress(Exception):
        await callback.answer()


@router.callback_query(F.data == "subscription:start:trial")
@router.callback_query(F.data == "subscription:pay:trial")
async def cb_subscription_start_trial(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    try:
        async with sessionmaker() as session:
            expires_at_utc = await activate_free_trial(session, user_id)
            tz = await get_user_tzinfo(session, user_id)
    except Exception as e:
        logger.warning("settings.start_trial.failed | user_id={} | err={}", user_id, e)
        await callback.message.answer("Пока не удалось активировать пробный период. Попробуйте позже.")
        await callback.answer()
        return

    if expires_at_utc is None:
        await callback.message.answer("Бесплатный пробный период уже использован. Можно перейти на платный тариф.")
        await cb_settings_open_subscription(callback)
        await callback.answer()
        return

    end_dt = expires_at_utc.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    text_out = (
        "Бесплатный пробный период активирован\n\n"
        f"Действует до: {end_dt}\n\n"
        "После окончания периода вы сможете выбрать месячный или годовой тариф."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать тарифы", callback_data="settings:open:subscription")],
    ])
    try:
        await callback.message.edit_text(text_out, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text_out, reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "subscription:pay:month")
async def cb_subscription_pay_month(callback: types.CallbackQuery, state: FSMContext) -> None:
    await _check_email_and_pay(callback, state, "month")


@router.callback_query(F.data == "subscription:pay:year")
async def cb_subscription_pay_year(callback: types.CallbackQuery, state: FSMContext) -> None:
    await _check_email_and_pay(callback, state, "year")


@router.callback_query(F.data == "subscription:email:cancel")
async def cb_subscription_email_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb_settings_open_subscription(callback)


@router.message(SubscriptionEmailStates.waiting_email)
async def handle_subscription_email(message: types.Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    email = message.text.strip()

    if not EMAIL_RE.match(email):
        await message.answer("Похоже, это не e-mail. Проверьте и отправьте: yourmail@example.ru")
        return

    async with sessionmaker() as session:
        await session.execute(update(UserModel).where(UserModel.id == user_id).values(email=email))
        await session.commit()

    await message.answer(f"E-mail сохранен: {email}")

    data = await state.get_data()
    plan = data.get("pending_plan", "month")
    await state.clear()

    if plan not in {"month", "year"}:
        await message.answer("E-mail сохранен. Можно выбрать тариф в разделе подписки.")
        return

    from bot.services.yookassa import create_payment

    plan_info = {
        "month": ("750 руб", "750"),
        "year": ("2500 руб", "2500"),
    }
    btn_text, _ = plan_info.get(plan, ("тариф", "0"))

    try:
        cp = await create_payment(user_id=user_id, plan=plan)
    except Exception as e:
        logger.error(f"Payment creation failed after email: {e}")
        await message.answer("Не удалось создать платеж. Попробуйте позже.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Оплатить {btn_text}", url=cp.confirmation_url)],
        [InlineKeyboardButton(text="Назад", callback_data="settings:open:subscription")],
    ])
    await message.answer("Перейдите по ссылке для оплаты:", reply_markup=kb, disable_web_page_preview=True)


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


