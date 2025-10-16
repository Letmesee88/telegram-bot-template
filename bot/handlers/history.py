from __future__ import annotations

from datetime import datetime, timedelta, timezone, date as date_cls, time as dtime
from typing import Any, List

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.i18n import gettext as _
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import MealModel, OnboardingAnswerModel
from bot.services.users import get_user_tzinfo
from bot.services.history import (
    aggregate_last7_days,
    get_week_advice,
    weekday_ru,
    set_add_in_day_target,
)
from bot.services.analytics import analytics
from bot.analytics.types import BaseEvent, EventProperties

router = Router(name="history")


def _kb_history_days(days: List[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, d in enumerate(days):
        ld: date_cls = d["date"]
        wd = weekday_ru(ld)
        text = f"{wd} {ld.strftime('%d.%m')}"
        if not d.get("has_entries"):
            text += " (пусто)"
        cb = f"history:day:{ld.isoformat()}"
        row.append(InlineKeyboardButton(text=_(text), callback_data=cb))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("history"))
async def cmd_history(message: types.Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id

    days, week_totals = await aggregate_last7_days(user_id)
    # Sort by recency: today first
    days_sorted = sorted(days, key=lambda x: x["date"], reverse=True)

    days_with = sum(1 for d in days_sorted if d.get("has_entries"))
    avg_cal = int((sum(d["total_cal"] for d in days_sorted) / days_with) if days_with else 0)
    avg_p = (sum(d["total_p"] for d in days_sorted) / days_with) if days_with else 0.0

    advice = await get_week_advice(user_id, days_sorted)

    lines: list[str] = []
    lines.append(_("История питания за 7 дней"))
    lines.append(_("📊 Коротко о главном:"))
    lines.append(_(f"🔥 Средние калории: {avg_cal} ккал"))
    lines.append(_(f"🥩 Средний белок: {avg_p:.1f} г"))
    lines.append(_(f"📅 Дней с записями: {days_with} из 7"))
    lines.append(_(f"✨ Совет: {advice if advice else 'временно недоступен'}"))

    kb = _kb_history_days(days_sorted)
    await message.answer("\n".join(lines), reply_markup=kb)

    # Analytics
    if analytics.logger and message.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=message.from_user.id,
                    event_type="HistoryOpened",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text=f"days_with={days_with}, avg_cal={avg_cal}, avg_p={avg_p:.1f}",
                        command="/history",
                    ),
                    language=message.from_user.language_code if message.from_user else None,
                )
            )
        except Exception:
            pass


@router.callback_query(F.data == "history:back")
async def cb_history_back(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    days, week_totals = await aggregate_last7_days(user_id)
    days_sorted = sorted(days, key=lambda x: x["date"], reverse=True)

    days_with = sum(1 for d in days_sorted if d.get("has_entries"))
    avg_cal = int((sum(d["total_cal"] for d in days_sorted) / days_with) if days_with else 0)
    avg_p = (sum(d["total_p"] for d in days_sorted) / days_with) if days_with else 0.0
    advice = await get_week_advice(user_id, days_sorted)

    lines = [
        _("История питания за 7 дней"),
        _("📊 Коротко о главном:"),
        _(f"🔥 Средние калории: {avg_cal} ккал"),
        _(f"🥩 Средний белок: {avg_p:.1f} г"),
        _(f"📅 Дней с записями: {days_with} из 7"),
        _(f"✨ Совет: {advice if advice else 'временно недоступен'}"),
    ]
    kb = _kb_history_days(days_sorted)
    try:
        await callback.message.edit_caption(caption="\n".join(lines), reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await callback.message.edit_text(text="\n".join(lines), reply_markup=kb)
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.regexp(r"^history:day:(\d{4}-\d{2}-\d{2})$"))
async def cb_history_day(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    m = (callback.data or "").split(":")
    if len(m) != 3:
        return
    try:
        target_local_date = datetime.fromisoformat(m[2]).date()
    except Exception:
        return

    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        start_local = datetime.combine(target_local_date, dtime(0, 0), tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        res = await session.execute(
            select(MealModel)
            .where(
                (MealModel.user_id == user_id)
                & (MealModel.status == "saved")
                & (MealModel.consumed_at >= start_utc)
                & (MealModel.consumed_at < end_utc)
            )
            .order_by(MealModel.consumed_at.asc())
        )
        meals = list(res.scalars().all())

        oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        plan = (oa.daily_plan if oa and isinstance(getattr(oa, "daily_plan", None), dict) else {}) or {}
        plan_cal = int(plan.get("calories") or 0)
        plan_p = float(plan.get("protein_g") or 0.0)
        plan_f = float(plan.get("fat_g") or 0.0)
        plan_c = float(plan.get("carbs_g") or 0.0)

    lines: list[str] = []
    lines.append(_("🗓 Дневник за {d}").format(d=start_local.strftime("%d.%m.%Y")))
    lines.append("")

    total_cal = sum(int(meal.calories or 0) for meal in meals)
    total_p = sum(float(meal.protein_g or 0.0) for meal in meals)
    total_f = sum(float(meal.fat_g or 0.0) for meal in meals)
    total_c = sum(float(meal.carbs_g or 0.0) for meal in meals)

    if meals:
        lines.append(_("Вы съели:"))
        lines.append("")
        for idx, meal in enumerate(meals, start=1):
            t_local = (meal.consumed_at or start_utc).astimezone(tz).strftime("%H:%M")
            title = (meal.title or _("Блюдо")).strip() or _("Блюдо")
            cal_i = int(meal.calories or 0)
            p_i = float(meal.protein_g or 0.0)
            f_i = float(meal.fat_g or 0.0)
            c_i = float(meal.carbs_g or 0.0)
            lines.append(f"{idx} {title} ({t_local})")
            lines.append(f"🔥 {cal_i} ккал | 🥩 {p_i:.1f} г | 🥑 {f_i:.1f} г | 🍞 {c_i:.1f} г")
            lines.append("")

    def _pct_raw(fact: float, plan_val: float) -> float:
        if plan_val > 0:
            return round((fact / plan_val) * 100.0, 1)
        return 0.0

    def _bar(pct: float) -> str:
        green = min(10, max(0, int(pct // 10)))
        return ("🟩" * green) + ("⬜️" * (10 - green))

    pct_cal_raw = _pct_raw(float(total_cal), float(plan_cal))
    pct_p_raw = _pct_raw(total_p, plan_p)
    pct_f_raw = _pct_raw(total_f, plan_f)
    pct_c_raw = _pct_raw(total_c, plan_c)

    lines.append(_("📈 Общая статистика:"))
    lines.append("")
    lines.append(_(f"🔥 Калории: {int(total_cal)} ккал / {int(plan_cal)} ккал ({pct_cal_raw:.1f} % )"))
    lines.append(_(f"🥩 Белки: {total_p:.1f} г / {plan_p:.1f} г ({pct_p_raw:.1f} % )"))
    lines.append(_(f"🥑 Жиры: {total_f:.1f} г / {plan_f:.1f} г ({pct_f_raw:.1f} % )"))
    lines.append(_(f"🍞 Углеводы: {total_c:.1f} г / {plan_c:.1f} г ({pct_c_raw:.1f} % )"))

    lines.append("")
    lines.append(_("📊 Прогресс:"))
    lines.append("")
    lines.append(f"🔥 {_bar(pct_cal_raw)} {pct_cal_raw:.1f} %")
    lines.append(f"🥩 {_bar(pct_p_raw)} {pct_p_raw:.1f} %")
    lines.append(f"🥑 {_bar(pct_f_raw)} {pct_f_raw:.1f} %")
    lines.append(f"🍞 {_bar(pct_c_raw)} {pct_c_raw:.1f} %")

    # Keyboard differs for today vs past
    today_local = datetime.now(tz).date()
    kb_rows: list[list[InlineKeyboardButton]] = []
    # Edit button always
    kb_rows.append([InlineKeyboardButton(text=_("✏️ Изменить блюда"), callback_data="de:l:1")])
    if target_local_date == today_local:
        # Today: only back to history
        kb_rows.append([InlineKeyboardButton(text=_("📖 К истории"), callback_data="history:back")])
    else:
        kb_rows.append([
            InlineKeyboardButton(text=_("➕ Добавить еду в этот день"), callback_data=f"history:add:{target_local_date.isoformat()}"),
        ])
        kb_rows.append([InlineKeyboardButton(text=_("📖 К истории"), callback_data="history:back")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        await callback.message.edit_caption(caption="\n".join(lines), reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await callback.message.edit_text(text="\n".join(lines), reply_markup=kb)
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb)

    # Analytics
    if analytics.logger and callback.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="HistoryDayViewed",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                        text=f"date={target_local_date.isoformat()}, cal={total_cal}",
                        command=None,
                    ),
                    language=getattr(callback.from_user, 'language_code', None),
                )
            )
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^history:add:(\d{4}-\d{2}-\d{2})$"))
async def cb_history_add(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    m = (callback.data or "").split(":")
    if len(m) != 3:
        return
    target = m[2]
    try:
        # validate (parse date and ignore the result)
        datetime.fromisoformat(target).date()
    except Exception:
        return

    await set_add_in_day_target(user_id, target)

    # UI prompt
    try:
        d_disp = datetime.fromisoformat(target).strftime("%d.%m.%Y")
    except Exception:
        d_disp = target
    text = _((f"Добавление еды в {d_disp} 🍗 Отправь фото еды или опиши текстом."))
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("◀️ Вернуться назад"), callback_data=f"history:day:{target}")]]
    )

    try:
        await callback.message.edit_caption(caption=text, reply_markup=kb)
        return
    except Exception:
        pass
    try:
        await callback.message.edit_text(text=text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)

    if analytics.logger and callback.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="HistoryAddInDayStarted",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                        text=f"date={target}",
                        command=None,
                    ),
                    language=getattr(callback.from_user, 'language_code', None),
                )
            )
        except Exception:
            pass
