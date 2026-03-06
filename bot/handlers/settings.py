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
        InlineKeyboardButton(text="рџ“Љ РЎСѓС‚РѕС‡РЅР°СЏ РЅРѕСЂРјР°", callback_data="settings:open:daily_norm"),
        InlineKeyboardButton(text="рџ“Њ РЁР°Р±Р»РѕРЅС‹ Р±Р»СЋРґ", callback_data="settings:open:templates"),
    ])
    rows.append([
        InlineKeyboardButton(text="рџ’Ћ РџРѕРґРїРёСЃРєР°", callback_data="settings:open:subscription"),
        InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:back:cabinet"),
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
            return dt.astimezone(tzinfo).strftime("%d.%m.%Y") if dt else "вЂ”"
        except Exception:
            return "вЂ”"
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
    plan_map = {"trial": "РџСЂРѕР±РЅС‹Р№ РґРѕСЃС‚СѓРї", "month": "РњРµСЃСЏС‡РЅР°СЏ РїРѕРґРїРёСЃРєР°", "year": "Р“РѕРґРѕРІР°СЏ РїРѕРґРїРёСЃРєР°"}
    base_plan = getattr(sub, "next_plan", None)
    base_plan = base_plan if base_plan in {"month", "year"} else sub.plan
    target = "year" if base_plan == "month" else "month"
    title = "рџ”„ РЎРјРµРЅР° РїР»Р°РЅР° РїРѕРґРїРёСЃРєРё"
    datetime.now(timezone.utc)
    try:
        cur_until = sub.expires_at_utc.astimezone(tzinfo).strftime("%d.%m.%Y")
    except Exception:
        cur_until = "вЂ”"
    lines = [
        title,
        "",
        f"РўРµРєСѓС‰РёР№ РїР»Р°РЅ: {plan_map.get(sub.plan, sub.plan)}",
        f"рџ“… Р”РµР№СЃС‚РІСѓРµС‚ РґРѕ: {cur_until}",
        "",
    ]
    if target == "year":
        lines += [
            "РџСЂРµРґР»Р°РіР°РµРјС‹Р№ РїР»Р°РЅ: Р“РѕРґРѕРІР°СЏ РїРѕРґРїРёСЃРєР°",
            "",
            "рџ’° РЎС‚РѕРёРјРѕСЃС‚СЊ: 2500 СЂСѓР±/РіРѕРґ",
            "",
            "РџСЂРё СЃРјРµРЅРµ РЅР° РіРѕРґРѕРІРѕР№ РїР»Р°РЅ:",
            "вЂў Р’С‹ СЃСЌРєРѕРЅРѕРјРёС‚Рµ 6 500 СЂСѓР±Р»РµР№ РІ РіРѕРґ",
            "вЂў РќРµ РЅСѓР¶РЅРѕ Р±РµСЃРїРѕРєРѕРёС‚СЊСЃСЏ Рѕ РµР¶РµРјРµСЃСЏС‡РЅС‹С… РїР»Р°С‚РµР¶Р°С…",
            "вЂў Р’СЃРµ С„СѓРЅРєС†РёРё РѕСЃС‚Р°СЋС‚СЃСЏ РґРѕСЃС‚СѓРїРЅС‹РјРё",
        ]
        cta = "рџ’Ћ РџРµСЂРµР№С‚Рё РЅР° РіРѕРґРѕРІСѓСЋ РїРѕРґРїРёСЃРєСѓ"
    else:
        lines += [
            "РџСЂРµРґР»Р°РіР°РµРјС‹Р№ РїР»Р°РЅ: РњРµСЃСЏС‡РЅР°СЏ РїРѕРґРїРёСЃРєР°",
            "",
            "рџ’° РЎС‚РѕРёРјРѕСЃС‚СЊ: 750 СЂСѓР±/РјРµСЃСЏС†",
        ]
        cta = "рџ’Ћ РџРµСЂРµР№С‚Рё РЅР° РјРµСЃСЏС‡РЅСѓСЋ РїРѕРґРїРёСЃРєСѓ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=cta, callback_data=f"subscription:change_plan_confirm:{target}")],
        [InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open:subscription")],
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
        "вњ… РђРІС‚РѕРїСЂРѕРґР»РµРЅРёРµ РѕС‚РєР»СЋС‡РµРЅРѕ",
        "",
        f"РџРѕРґРїРёСЃРєР° РѕСЃС‚Р°РµС‚СЃСЏ Р°РєС‚РёРІРЅРѕР№ РґРѕ {until.strftime('%d.%m.%Y')}",
        f"РћСЃС‚Р°Р»РѕСЃСЊ РґРЅРµР№: {days_left}",
        "",
        "РђРІС‚РѕРјР°С‚РёС‡РµСЃРєРѕРµ РїСЂРѕРґР»РµРЅРёРµ Р±РѕР»СЊС€Рµ РЅРµ Р±СѓРґРµС‚ РїСЂРѕРёСЃС…РѕРґРёС‚СЊ.",
        "Р’С‹ РјРѕР¶РµС‚Рµ РІРѕР·РѕР±РЅРѕРІРёС‚СЊ РїРѕРґРїРёСЃРєСѓ РІ Р»СЋР±РѕРµ РІСЂРµРјСЏ.",
        "",
        "РЎРїР°СЃРёР±Рѕ Р·Р° РёСЃРїРѕР»СЊР·РѕРІР°РЅРёРµ Calorissimo ai! рџЌ¬",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="в†”пёЏ РЎРјРµРЅРёС‚СЊ РїР»Р°РЅ", callback_data="subscription:change_plan")],
        [InlineKeyboardButton(text="вњ… Р’РєР»СЋС‡РёС‚СЊ Р°РІС‚РѕРїСЂРѕРґР»РµРЅРёРµ", callback_data="subscription:autorenew:enable")],
        [InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open:subscription")],
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
        "вњ… РђРІС‚РѕРїСЂРѕРґР»РµРЅРёРµ РІРєР»СЋС‡РµРЅРѕ",
        "",
        f"РџРѕРґРїРёСЃРєР° Р±СѓРґРµС‚ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РїСЂРѕРґР»РµРЅР° {until.strftime('%d.%m.%Y')}",
        f"РћСЃС‚Р°Р»РѕСЃСЊ РґРЅРµР№: {days_left}",
        "",
        "РЎРїР°СЃРёР±Рѕ, С‡С‚Рѕ РѕСЃС‚Р°РµС‚РµСЃСЊ СЃ РЅР°РјРё! рџЋ‰",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="в†”пёЏ РЎРјРµРЅРёС‚СЊ РїР»Р°РЅ", callback_data="subscription:change_plan")],
        [InlineKeyboardButton(text="вќЊ РћС‚РєР»СЋС‡РёС‚СЊ РїРѕРґРїРёСЃРєСѓ", callback_data="subscription:autorenew:disable")],
        [InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open:subscription")],
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
    plan_map = {"trial": "РџСЂРѕР±РЅС‹Р№ РґРѕСЃС‚СѓРї", "month": "РњРµСЃСЏС‡РЅР°СЏ РїРѕРґРїРёСЃРєР°", "year": "Р“РѕРґРѕРІР°СЏ РїРѕРґРїРёСЃРєР°"}
    cur_plan = plan_map.get(sub.plan, sub.plan)
    new_plan = plan_map.get(target, target)
    until = sub.expires_at_utc.astimezone(tzinfo).strftime("%d.%m.%Y")
    lines = [
        "вњ… РџРѕРґС‚РІРµСЂР¶РґРµРЅРёРµ СЃРјРµРЅС‹ РїР»Р°РЅР°",
        "",
        f"РЎРµР№С‡Р°СЃ: {cur_plan}",
        f"РќР°: {new_plan}",
        "",
        "рџ“… РљРѕРіРґР° РїСЂРѕРёР·РѕР№РґРµС‚ СЃРјРµРЅР°:",
        f"Р’ РєРѕРЅС†Рµ С‚РµРєСѓС‰РµРіРѕ РїРµСЂРёРѕРґР° ({until})",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="вњ… РџРѕРґС‚РІРµСЂРґРёС‚СЊ СЃРјРµРЅСѓ", callback_data=f"subscription:change_plan_apply:{target}")],
        [InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="subscription:change_plan")],
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
    plan_map = {"trial": "РџСЂРѕР±РЅС‹Р№ РґРѕСЃС‚СѓРї", "month": "РњРµСЃСЏС‡РЅР°СЏ РїРѕРґРїРёСЃРєР°", "year": "Р“РѕРґРѕРІР°СЏ РїРѕРґРїРёСЃРєР°"}
    new_plan = plan_map.get(target, target)
    until = "вЂ”"
    try:
        async with sessionmaker() as session:
            sub2 = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
            if sub2 and sub2.expires_at_utc:
                tzinfo = await get_user_tzinfo(session, user_id)
                until = sub2.expires_at_utc.astimezone(tzinfo).strftime("%d.%m.%Y")
    except Exception:
        pass
    lines = [
        "рџЋ‰ РџР»Р°РЅ СѓСЃРїРµС€РЅРѕ РёР·РјРµРЅРµРЅ!",
        "",
        f"РќРѕРІС‹Р№ РїР»Р°РЅ: {new_plan}",
        f"рџ“… Р”Р°С‚Р° СЃРїРёСЃР°РЅРёСЏ: {until}",
        "вњ… РЎРјРµРЅР° РїР»Р°РЅР° Р·Р°РІРµСЂС€РµРЅР°!",
        "",
        "РЎР»РµРґСѓСЋС‰РёР№ РїР»Р°С‚РµР¶ Р±СѓРґРµС‚ РїРѕ РЅРѕРІРѕРјСѓ С‚Р°СЂРёС„Сѓ.",
    ]
    # Actions after success
    async with sessionmaker() as session:
        sub = await session.scalar(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
    kb_rows = [[InlineKeyboardButton(text="в†”пёЏ РЎРјРµРЅРёС‚СЊ РїР»Р°РЅ", callback_data="subscription:change_plan")]]
    if sub and bool(getattr(sub, "auto_renew", True)):
        kb_rows.append([InlineKeyboardButton(text="вќЊ РћС‚РєР»СЋС‡РёС‚СЊ РїРѕРґРїРёСЃРєСѓ", callback_data="subscription:autorenew:disable")])
    else:
        kb_rows.append([InlineKeyboardButton(text="вњ… Р’РєР»СЋС‡РёС‚СЊ Р°РІС‚РѕРїСЂРѕРґР»РµРЅРёРµ", callback_data="subscription:autorenew:enable")])
    kb_rows.append([InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open:subscription")])
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
            "рџ“Љ РЎСѓС‚РѕС‡РЅР°СЏ РЅРѕСЂРјР°\n\n"
            "РќРµ РЅР°С€С‘Р» Р±Р°Р·РѕРІС‹Рµ РґР°РЅРЅС‹Рµ. РџСЂРѕР№РґРё РєРѕСЂРѕС‚РєСѓСЋ РЅР°СЃС‚СЂРѕР№РєСѓ: /start"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open")]]
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
    remain_str = "РЅРµС‚ РґР°РЅРЅС‹С…"
    goal_str = "РЅРµС‚ РґР°РЅРЅС‹С…"
    try:
        if goal_weight is not None:
            goal_str = f"{float(goal_weight)} РєРі"
        if cw is not None and goal_weight is not None:
            remain = abs(float(cw) - float(goal_weight))
            remain_str = f"{round(remain, 1)} РєРі"
    except Exception:
        pass

    text = (
        "рџ“Љ РЎСѓС‚РѕС‡РЅР°СЏ РЅРѕСЂРјР°\n\n"
        f"рџ”Ґ РљР°Р»РѕСЂРёРё: {cal_val} РєРєР°Р»\n"
        f"рџҐ© Р‘РµР»РєРё: {p_val} Рі\n"
        f"рџҐ‘ Р–РёСЂС‹: {f_val} Рі\n"
        f"рџЌћ РЈРіР»РµРІРѕРґС‹: {c_val} Рі\n\n"
        f"рџЋЇ Р¦РµР»СЊ: {goal_str} | Р”Рѕ С†РµР»Рё: {remain_str}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="вњЏпёЏ РР·РјРµРЅРёС‚СЊ РїР»Р°РЅ РїРёС‚Р°РЅРёСЏ", callback_data="daily_norm:edit_start"), InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open")]]
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
    text = "РќР°РїРёС€Рё, РІ СЃРІРѕР±РѕРґРЅРѕРј С„РѕСЂРјР°С‚Рµ, С‡С‚Рѕ РЅСѓР¶РЅРѕ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°С‚СЊ РІ С‚РІРѕС‘Рј РёРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРј РїР»Р°РЅРµ"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open:daily_norm")]]
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
        await message.answer(_("вњЁ РР·СѓС‡Р°СЋ РІР°С€Рё РїРѕР¶РµР»Р°РЅРёСЏ Рё РѕР±РЅРѕРІР»СЏСЋ РїР»Р°РЅ..."))

    try:
        async with sessionmaker() as session:
            existing = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
            )
            if not existing:
                await message.answer(_("РќРµ РЅР°С€С‘Р» Р±Р°Р·РѕРІС‹Рµ РґР°РЅРЅС‹Рµ. РџСЂРѕР№РґРё РєРѕСЂРѕС‚РєСѓСЋ РЅР°СЃС‚СЂРѕР№РєСѓ: /start"))
                await state.clear()
                return

            data_json = dict(existing.data or {})
            dp_json = dict(existing.daily_plan or {})

            # 2) Validate payload first
            try:
                payload = OnboardingData.model_validate(data_json)
            except Exception as e:
                logger.warning("settings.daily_norm.payload_invalid | user_id={} | err={}", user_id, e)
                await message.answer(_("Р”Р°РЅРЅС‹Рµ РїРѕРІСЂРµР¶РґРµРЅС‹. РџРѕРїСЂРѕР±СѓР№ Р·Р°РЅРѕРІРѕ: /start"))
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
                    await message.answer(_("РќРµ РґРѕ РєРѕРЅС†Р° РїРѕРЅСЏР» Р·Р°РїСЂРѕСЃ. РЎС„РѕСЂРјСѓР»РёСЂСѓР№ РѕРґРЅРѕР№ С„СЂР°Р·РѕР№, РЅР°РїСЂРёРјРµСЂ: \nвЂў 'СѓРјРµРЅСЊС€Рё СѓРіР»РµРІРѕРґС‹ РЅР° 10%' \nвЂў 'С…РѕС‡Сѓ Р±С‹СЃС‚СЂРµРµ РїРѕС…СѓРґРµС‚СЊ' \nвЂў 'Рє 01.03.2026' \nвЂў 'РјР°Р»Рѕ РґРІРёРіР°СЋСЃСЊ вЂ” РїРѕСЃС‚Р°РІСЊ РЅРёР·РєСѓСЋ Р°РєС‚РёРІРЅРѕСЃС‚СЊ'"))
                    return
                h = parse_adjustment_heuristic(text_raw)
                if h:
                    parsed = h
                else:
                    await message.answer(_("РќРµ РґРѕ РєРѕРЅС†Р° РїРѕРЅСЏР» Р·Р°РїСЂРѕСЃ. РЎС„РѕСЂРјСѓР»РёСЂСѓР№ РѕРґРЅРѕР№ С„СЂР°Р·РѕР№, РЅР°РїСЂРёРјРµСЂ: \nвЂў 'СѓР±РµСЂРёС‚Рµ СѓРіР»РµРІРѕРґС‹' \nвЂў 'РґРѕР±Р°РІСЊ 200 РєРєР°Р»' \nвЂў 'РјР°Р»Рѕ РґРІРёРіР°СЋСЃСЊ вЂ” РїРѕСЃС‚Р°РІСЊ РЅРёР·РєСѓСЋ Р°РєС‚РёРІРЅРѕСЃС‚СЊ'"))
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
        await message.answer(_("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РёР·РјРµРЅРµРЅРёСЏ. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ."))
        await state.clear()
        return

    personal_line: str | None = None
    try:
        note = getattr(parsed, "rationale", None)
        intents = list(getattr(parsed, "intents", []) or [])
        if isinstance(note, str) and note.strip():
            personal_line = "РЈС‡С‘Р» Р·Р°РїСЂРѕСЃ: " + note.strip()
        else:
            intent_map = {
                "low_fodmap_candidate": "СѓРјРµРЅСЊС€РёС‚СЊ FODMAP-РїСЂРѕРґСѓРєС‚С‹",
                "lactose_free": "РёР·Р±РµРіР°С‚СЊ Р»Р°РєС‚РѕР·С‹",
                "gluten_free": "Р±РµР· РіР»СЋС‚РµРЅР°",
                "sugar_free": "РѕРіСЂР°РЅРёС‡РёС‚СЊ СЃР°С…Р°СЂ",
                "keto": "РєРµС‚Рѕ-СЃС…РµРјСѓ",
                "low_carb": "СЃРЅРёР·РёС‚СЊ СѓРіР»РµРІРѕРґС‹",
                "high_protein": "Р°РєС†РµРЅС‚ РЅР° Р±РµР»РѕРє",
                "raise_calories": "СѓРІРµР»РёС‡РёС‚СЊ РєР°Р»РѕСЂРёР№РЅРѕСЃС‚СЊ",
                "lower_calories": "СЃРЅРёР·РёС‚СЊ РєР°Р»РѕСЂРёР№РЅРѕСЃС‚СЊ",
                "activity_down": "РїРѕРЅРёР·РёС‚СЊ Р°РєС‚РёРІРЅРѕСЃС‚СЊ",
                "activity_up": "РїРѕРІС‹СЃРёС‚СЊ Р°РєС‚РёРІРЅРѕСЃС‚СЊ",
                "reduce_protein": "СЃРЅРёР·РёС‚СЊ Р±РµР»РѕРє",
                "reduce_fat": "СЃРЅРёР·РёС‚СЊ Р¶РёСЂС‹",
                "increase_fat": "РїРѕРІС‹СЃРёС‚СЊ Р¶РёСЂС‹",
                "custom_macros": "РєР°СЃС‚РѕРјРЅС‹Рµ РјР°РєСЂРѕСЃС‹",
            }
            phrases = [intent_map[i] for i in intents if i in intent_map]
            if phrases:
                personal_line = "РЈС‡С‘Р» Р·Р°РїСЂРѕСЃ: " + ", ".join(phrases)
    except Exception:
        personal_line = None

    lines: list[str] = []
    lines.append("<b>РўРІРѕР№ РїР»Р°РЅ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°РЅ!</b>")
    lines.append("")
    try:
        if payload.goal != Goal.maintain:
            if getattr(new_plan, "eta_date", None) is not None and getattr(payload, "goal_weight_kg", None) is not None:
                delta = abs(float(payload.weight_kg) - float(payload.goal_weight_kg))
                formatted_date = new_plan.eta_date.strftime("%d.%m.%Y")
                if payload.goal == Goal.lose:
                    lines.append(f"РўС‹ СЃР±СЂРѕСЃРёС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
                elif payload.goal == Goal.gain:
                    lines.append(f"РўС‹ РЅР°Р±РµСЂРµС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
            lines.append(f"РЎРєРѕСЂРѕСЃС‚СЊ: {getattr(new_plan, 'weekly_rate_kg', 0)} РєРі РІ РЅРµРґРµР»СЋ")
    except Exception:
        pass
    lines.append("")
    lines.append("<b>РћР±РЅРѕРІР»РµРЅРЅР°СЏ РґРЅРµРІРЅР°СЏ РЅРѕСЂРјР°:</b>")
    lines.append(f"рџ”Ґ РљР°Р»РѕСЂРёРё: {new_plan.calories} РєРєР°Р»")
    lines.append(f"рџҐ© Р‘РµР»РєРё: {new_plan.protein_g} Рі")
    lines.append(f"рџҐ‘ Р–РёСЂС‹: {new_plan.fat_g} Рі")
    lines.append(f"рџЌћ РЈРіР»РµРІРѕРґС‹: {new_plan.carbs_g} Рі")
    lines.append("")
    if personal_line:
        def _norm_txt(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").lower()).strip()
        pl_core = re.sub(r"^СѓС‡[РµС‘]Р»\s+Р·Р°РїСЂРѕСЃ:\s*", "", personal_line, flags=re.IGNORECASE)
        if _norm_txt(pl_core) and _norm_txt(pl_core) not in _norm_txt(explanation):
            lines.append(personal_line)
    lines.append(explanation)
    lines.append("")
    lines.append("РћСЃС‚Р°РІРёРј С‚Р°Рє РёР»Рё РЅСѓР¶РЅР° РµС‰Рµ РєРѕСЂСЂРµРєС‚РёСЂРѕРІРєР°?")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="РћС‚Р»РёС‡РЅРѕ", callback_data="daily_norm:final:ok")],
            [InlineKeyboardButton(text="РҐРѕС‡Сѓ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°С‚СЊ", callback_data="daily_norm:final:adjust")],
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
                    new_lines.append("<b>РўРІРѕР№ РїР»Р°РЅ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°РЅ!</b>")
                    new_lines.append("")
                    try:
                        if payload.goal != Goal.maintain:
                            if getattr(new_plan, "eta_date", None) is not None and getattr(payload, "goal_weight_kg", None) is not None:
                                delta2 = abs(float(payload.weight_kg) - float(payload.goal_weight_kg))
                                formatted_date2 = new_plan.eta_date.strftime("%d.%m.%Y")
                                if payload.goal == Goal.lose:
                                    new_lines.append(f"РўС‹ СЃР±СЂРѕСЃРёС€СЊ {round(delta2, 1)} РєРі Рє {formatted_date2}")
                                elif payload.goal == Goal.gain:
                                    new_lines.append(f"РўС‹ РЅР°Р±РµСЂРµС€СЊ {round(delta2, 1)} РєРі Рє {formatted_date2}")
                            new_lines.append(f"РЎРєРѕСЂРѕСЃС‚СЊ: {getattr(new_plan, 'weekly_rate_kg', 0)} РєРі РІ РЅРµРґРµР»СЋ")
                    except Exception:
                        pass
                    new_lines.append("")
                    new_lines.append("<b>РћР±РЅРѕРІР»РµРЅРЅР°СЏ РґРЅРµРІРЅР°СЏ РЅРѕСЂРјР°:</b>")
                    new_lines.append(f"рџ”Ґ РљР°Р»РѕСЂРёРё: {new_plan.calories} РєРєР°Р»")
                    new_lines.append(f"рџҐ© Р‘РµР»РєРё: {new_plan.protein_g} Рі")
                    new_lines.append(f"рџҐ‘ Р–РёСЂС‹: {new_plan.fat_g} Рі")
                    new_lines.append(f"рџЌћ РЈРіР»РµРІРѕРґС‹: {new_plan.carbs_g} Рі")
                    new_lines.append("")
                    if personal_line:
                        def _norm_txt2(s: str) -> str:
                            return re.sub(r"\s+", " ", (s or "").lower()).strip()
                        pl_core2 = re.sub(r"^СѓС‡[РµС‘]Р»\s+Р·Р°РїСЂРѕСЃ:\s*", "", personal_line, flags=re.IGNORECASE)
                        if _norm_txt2(pl_core2) and _norm_txt2(pl_core2) not in _norm_txt2(rewritten):
                            new_lines.append(personal_line)
                    new_lines.append(rewritten)
                    new_lines.append("")
                    new_lines.append("РћСЃС‚Р°РІРёРј С‚Р°Рє РёР»Рё РЅСѓР¶РЅР° РµС‰Рµ РєРѕСЂСЂРµРєС‚РёСЂРѕРІРєР°?")
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
    text = "РќР°РїРёС€Рё, РІ СЃРІРѕР±РѕРґРЅРѕРј С„РѕСЂРјР°С‚Рµ, С‡С‚Рѕ РЅСѓР¶РЅРѕ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°С‚СЊ РІ С‚РІРѕС‘Рј РёРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРј РїР»Р°РЅРµ"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="settings:open:daily_norm")]]
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
        lines.append("Управление подпиской")
        lines.append("")
        lines.append("У вас нет активной подписки.")
        lines.append("")
        lines.append("Доступные тарифы:")

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
        "Тариф Месяц\n\n"
        "План: месячный\n"
        "Стоимость: 750 руб / месяц\n"
        "Период: 30 дней\n\n"
        "После оплаты подписка продлевается автоматически.\n\n"
        "Нажимая кнопку оплаты, вы соглашаетесь с офертой и политикой конфиденциальности, а также на регулярные списания до отключения."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 750 руб", callback_data="subscription:pay:month")],
        [InlineKeyboardButton(text="Назад", callback_data="settings:open:subscription")],
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
        "Тариф Месяц\n\n"
        "План: годовой\n"
        "Стоимость: 2500 руб / год\n"
        "Период: 365 дней\n\n"
        "После оплаты подписка продлевается автоматически.\n\n"
        "Нажимая кнопку оплаты, вы соглашаетесь с офертой и политикой конфиденциальности, а также на регулярные списания до отключения."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 2500 руб", callback_data="subscription:pay:year")],
        [InlineKeyboardButton(text="Назад", callback_data="settings:open:subscription")],
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
    rows.append([InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="templates:back:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "settings:open:templates")
async def cb_settings_open_templates(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        counts = await list_categories_with_counts(session, user_id)
    text = _("Р’С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ РїСЂРёС‘РјР° РїРёС‰Рё:")
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


