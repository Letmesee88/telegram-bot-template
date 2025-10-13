from datetime import datetime, timezone, timedelta

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.i18n import gettext as _
from sqlalchemy import select

from bot.keyboards.inline.menu import main_keyboard
from bot.database.database import sessionmaker
from bot.database.models import MealModel, OnboardingAnswerModel
from bot.services.users import get_user_tzinfo

router = Router(name="menu")


@router.message(Command(commands=["menu", "main"]))
async def menu_handler(message: types.Message) -> None:
    """Return main menu."""
    await message.answer(_("title main keyboard"), reply_markup=main_keyboard())


async def _edit_caption_or_text(cb: types.CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Edit caption/text if possible, else send a new message (for callbacks)."""
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
        pass


@router.message(Command("day"))
async def cmd_day(message: types.Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    PAGE_SIZE = 10

    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        now_local = datetime.now(tz)
        local_date = now_local.date()
        local_start = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)

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

        oa = await session.scalar(
            select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
        )
        plan = (oa.daily_plan if oa and isinstance(getattr(oa, "daily_plan", None), dict) else {}) or {}
        plan_cal = int(plan.get("calories") or 0)
        plan_p = float(plan.get("protein_g") or 0.0)
        plan_f = float(plan.get("fat_g") or 0.0)
        plan_c = float(plan.get("carbs_g") or 0.0)

    date_str = local_start.strftime("%d.%m.%Y")
    lines: list[str] = [_("🗓 Дневник за {d}").format(d=date_str), ""]

    total_cal = sum(int(meal.calories or 0) for meal in meals)
    total_p = sum(float(meal.protein_g or 0.0) for meal in meals)
    total_f = sum(float(meal.fat_g or 0.0) for meal in meals)
    total_c = sum(float(meal.carbs_g or 0.0) for meal in meals)

    n = len(meals)
    total_pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
    page = 1
    start_idx = (page - 1) * PAGE_SIZE
    end_idx = min(n, start_idx + PAGE_SIZE)

    if n > 0:
        lines.append(_("Вы съели:"))
        lines.append("")
        for idx, meal in enumerate(meals[start_idx:end_idx], start=start_idx + 1):
            t_local = (meal.consumed_at or start_utc).astimezone(tz).strftime("%H:%M")
            title = (meal.title or _("Блюдо")).strip() or _("Блюдо")
            cal_i = int(meal.calories or 0)
            p_i = float(meal.protein_g or 0.0)
            f_i = float(meal.fat_g or 0.0)
            c_i = float(meal.carbs_g or 0.0)
            lines.append(f"{idx}. {title} ({t_local})")
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
    lines.append(_("🔥 Калории: {} ккал / {} ккал ({} %)".format(int(total_cal), int(plan_cal), f"{pct_cal_raw:.1f}")))
    lines.append(_("🥩 Белки: {} г / {} г ({} %)".format(f"{total_p:.1f}", f"{plan_p:.1f}", f"{pct_p_raw:.1f}")))
    lines.append(_("🥑 Жиры: {} г / {} г ({} %)".format(f"{total_f:.1f}", f"{plan_f:.1f}", f"{pct_f_raw:.1f}")))
    lines.append(_("🍞 Углеводы: {} г / {} г ({} %)".format(f"{total_c:.1f}", f"{plan_c:.1f}", f"{pct_c_raw:.1f}")))
    lines.append("")

    lines.append(_("📊 Прогресс:"))
    lines.append("")
    lines.append(f"🔥 {_bar(pct_cal_raw)} {pct_cal_raw:.1f} %")
    lines.append(f"🥩 {_bar(pct_p_raw)} {pct_p_raw:.1f} %")
    lines.append(f"🥑 {_bar(pct_f_raw)} {pct_f_raw:.1f} %")
    lines.append(f"🍞 {_bar(pct_c_raw)} {pct_c_raw:.1f} %")

    text = "\n".join(lines)

    kb_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if total_pages > 1 and page < total_pages:
        nav_row.append(InlineKeyboardButton(text=_("Вперёд ▶️"), callback_data=f"diary:today:{page+1}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None

    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^diary:today:(\d+)$"))
async def cb_diary_today(callback: types.CallbackQuery) -> None:
    m = (callback.data or "").split(":")
    if len(m) != 3 or not callback.from_user:
        return
    try:
        page = max(1, int(m[2]))
    except Exception:
        page = 1
    user_id = callback.from_user.id
    PAGE_SIZE = 10

    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        now_local = datetime.now(tz)
        local_date = now_local.date()
        local_start = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)

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

        oa = await session.scalar(
            select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
        )
        plan = (oa.daily_plan if oa and isinstance(getattr(oa, "daily_plan", None), dict) else {}) or {}
        plan_cal = int(plan.get("calories") or 0)
        plan_p = float(plan.get("protein_g") or 0.0)
        plan_f = float(plan.get("fat_g") or 0.0)
        plan_c = float(plan.get("carbs_g") or 0.0)

    date_str = local_start.strftime("%d.%m.%Y")
    lines: list[str] = [_("🗓 Дневник за {d}").format(d=date_str), ""]

    total_cal = sum(int(meal.calories or 0) for meal in meals)
    total_p = sum(float(meal.protein_g or 0.0) for meal in meals)
    total_f = sum(float(meal.fat_g or 0.0) for meal in meals)
    total_c = sum(float(meal.carbs_g or 0.0) for meal in meals)

    n = len(meals)
    total_pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * PAGE_SIZE
    end_idx = min(n, start_idx + PAGE_SIZE)

    if n > 0:
        lines.append(_("Вы съели:"))
        lines.append("")
        for idx, meal in enumerate(meals[start_idx:end_idx], start=start_idx + 1):
            t_local = (meal.consumed_at or start_utc).astimezone(tz).strftime("%H:%M")
            title = (meal.title or _("Блюдо")).strip() or _("Блюдо")
            cal_i = int(meal.calories or 0)
            p_i = float(meal.protein_g or 0.0)
            f_i = float(meal.fat_g or 0.0)
            c_i = float(meal.carbs_g or 0.0)
            lines.append(f"{idx}. {title} ({t_local})")
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
    lines.append(_("🔥 Калории: {} ккал / {} ккал ({} %)".format(int(total_cal), int(plan_cal), f"{pct_cal_raw:.1f}")))
    lines.append(_("🥩 Белки: {} г / {} г ({} %)".format(f"{total_p:.1f}", f"{plan_p:.1f}", f"{pct_p_raw:.1f}")))
    lines.append(_("🥑 Жиры: {} г / {} г ({} %)".format(f"{total_f:.1f}", f"{plan_f:.1f}", f"{pct_f_raw:.1f}")))
    lines.append(_("🍞 Углеводы: {} г / {} г ({} %)".format(f"{total_c:.1f}", f"{plan_c:.1f}", f"{pct_c_raw:.1f}")))
    lines.append("")

    lines.append(_("📊 Прогресс:"))
    lines.append("")
    lines.append(f"🔥 {_bar(pct_cal_raw)} {pct_cal_raw:.1f} %")
    lines.append(f"🥩 {_bar(pct_p_raw)} {pct_p_raw:.1f} %")
    lines.append(f"🥑 {_bar(pct_f_raw)} {pct_f_raw:.1f} %")
    lines.append(f"🍞 {_bar(pct_c_raw)} {pct_c_raw:.1f} %")

    text = "\n".join(lines)

    kb_rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if total_pages > 1 and page > 1:
        nav_row.append(InlineKeyboardButton(text=_("◀️ Назад"), callback_data=f"diary:today:{page-1}"))
    if total_pages > 1 and page < total_pages:
        nav_row.append(InlineKeyboardButton(text=_("Вперёд ▶️"), callback_data=f"diary:today:{page+1}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None

    await _edit_caption_or_text(callback, text, kb=kb)
