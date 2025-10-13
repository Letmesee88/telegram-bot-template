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
from bot.services.analytics import analytics
from bot.analytics.types import BaseEvent, EventProperties

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
    edit_row: list[InlineKeyboardButton] = []
    nav_row: list[InlineKeyboardButton] = []
    # Always show edit button
    edit_row.append(InlineKeyboardButton(text=_("✏️ Изменить блюда"), callback_data="de:l:1"))
    kb_rows.append(edit_row)
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
    edit_row: list[InlineKeyboardButton] = []
    nav_row: list[InlineKeyboardButton] = []
    edit_row.append(InlineKeyboardButton(text=_("✏️ Изменить блюда"), callback_data="de:l:1"))
    kb_rows.append(edit_row)
    if total_pages > 1 and page > 1:
        nav_row.append(InlineKeyboardButton(text=_("◀️ Назад"), callback_data=f"diary:today:{page-1}"))
    if total_pages > 1 and page < total_pages:
        nav_row.append(InlineKeyboardButton(text=_("Вперёд ▶️"), callback_data=f"diary:today:{page+1}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None

    await _edit_caption_or_text(callback, text, kb=kb)


@router.callback_query(F.data.regexp(r"^de:del:(\d+)(?::(\d+))?$"))
async def cb_edit_delete(callback: types.CallbackQuery) -> None:
    m = (callback.data or "").split(":")
    if len(m) < 3 or not callback.from_user:
        return
    meal_id = int(m[2])
    page = int(m[3]) if len(m) >= 4 and m[3].isdigit() else 1
    user_id = callback.from_user.id

    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return
        meal.status = "deleted"
        await session.commit()

    # Analytics: Deleted
    if analytics.logger and callback.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="DiaryEdit:Deleted",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                        text=f"meal_id={meal_id}",
                        command=None,
                    ),
                    language=getattr(callback.from_user, 'language_code', None),
                )
            )
        except Exception:
            pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("◀️ К списку блюд"), callback_data=f"de:l:{page}")]]
    )
    await _edit_caption_or_text(callback, _("🗑Блюдо удалено"), kb=kb)
    await callback.answer()


@router.callback_query(F.data == "de:back:day")
async def cb_back_to_day(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
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
    edit_row: list[InlineKeyboardButton] = []
    nav_row: list[InlineKeyboardButton] = []
    edit_row.append(InlineKeyboardButton(text=_("✏️ Изменить блюда"), callback_data="de:l:1"))
    kb_rows.append(edit_row)
    if total_pages > 1 and page < total_pages:
        nav_row.append(InlineKeyboardButton(text=_("Вперёд ▶️"), callback_data=f"diary:today:{page+1}"))
    if nav_row:
        kb_rows.append(nav_row)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None

    await _edit_caption_or_text(callback, text, kb=kb)


@router.callback_query(F.data.regexp(r"^de:l:(\d+)$"))
async def cb_edit_list(callback: types.CallbackQuery) -> None:
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

    n = len(meals)
    total_pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * PAGE_SIZE
    end_idx = min(n, start_idx + PAGE_SIZE)

    date_str = local_start.strftime("%d.%m.%Y")
    lines: list[str] = [_("👉🏻 Выберите блюдо которое нужно отредактировать"), ""]
    for i, meal in enumerate(meals[start_idx:end_idx], start=start_idx + 1):
        t_local = (meal.consumed_at or start_utc).astimezone(tz).strftime("%H:%M")
        title = (meal.title or _("Блюдо")).strip() or _("Блюдо")
        cal_i = int(meal.calories or 0)
        lines.append(f"{i} {title} ({t_local}) → {cal_i} ккал")

    text = "\n".join(lines) if lines else _("На сегодня нет сохранённых блюд")

    kb_rows: list[list[InlineKeyboardButton]] = []
    if n > 0:
        for i, meal in enumerate(meals[start_idx:end_idx], start=start_idx + 1):
            title_short = ((meal.title or _("Блюдо")).strip() or _("Блюдо"))
            title_short = (title_short[:18] + "…") if len(title_short) > 19 else title_short
            kb_rows.append([InlineKeyboardButton(text=f"{i} {title_short}", callback_data=f"de:d:{meal.id}:{page}")])

    nav_row: list[InlineKeyboardButton] = []
    if total_pages > 1 and page > 1:
        nav_row.append(InlineKeyboardButton(text=_("◀️ Назад"), callback_data=f"de:l:{page-1}"))
    if total_pages > 1 and page < total_pages:
        nav_row.append(InlineKeyboardButton(text=_("Вперёд ▶️"), callback_data=f"de:l:{page+1}"))
    if nav_row:
        kb_rows.append(nav_row)

    back_row = [InlineKeyboardButton(text=_("◀️ Вернуться назад"), callback_data="de:back:day")]
    kb_rows.append(back_row)

    # Analytics: List opened
    if analytics.logger and callback.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="DiaryEdit:ListOpened",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                        text=f"page={page}, count={n}",
                        command=None,
                    ),
                    language=getattr(callback.from_user, 'language_code', None),
                )
            )
        except Exception:
            pass

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _edit_caption_or_text(callback, text, kb=kb)


@router.callback_query(F.data.regexp(r"^de:d:(\d+)(?::(\d+))?$"))
async def cb_edit_detail(callback: types.CallbackQuery) -> None:
    m = (callback.data or "").split(":")
    if len(m) < 3 or not callback.from_user:
        return
    meal_id = int(m[2])
    page = int(m[3]) if len(m) >= 4 and m[3].isdigit() else 1
    user_id = callback.from_user.id

    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return
        # Eagerly access items list before session closes
        items = []
        try:
            for it in (meal.items or []):
                items.append(it)
        except Exception:
            items = []

    title = (meal.title or _("Блюдо")).strip() or _("Блюдо")
    cal = int(meal.calories or 0)
    p = float(meal.protein_g or 0.0)
    f = float(meal.fat_g or 0.0)
    c = float(meal.carbs_g or 0.0)
    w = float(meal.weight_g or 0.0)

    lines: list[str] = [title, ""]
    lines.append(_("🍜 Состав:"))
    if items:
        for it in items:
            try:
                name = (it.name or _("Ингредиент")).strip() or _("Ингредиент")
                segs: list[str] = []
                if it.weight_g is not None:
                    segs.append(f"{float(it.weight_g):g} г")
                if it.calories is not None:
                    segs.append(f"{int(float(it.calories))} ккал")
                if segs:
                    lines.append(f"• {name} ( " + ", ".join(segs) + " )")
                else:
                    lines.append(f"• {name}")
            except Exception:
                continue
    else:
        lines.append("• …")

    lines.append("")
    lines.append(_("🔥 Калории: {cal} ккал | 🥩 Белки: {p} г | 🥑 Жиры: {f} г | 🍞 Углеводы: {c} г").format(cal=cal, p=f"{p:.1f}", f=f"{f:.1f}", c=f"{c:.1f}"))
    if w:
        lines.append("")
        lines.append(_("⚖️ Вес: {w} г").format(w=f"{w:.1f}"))

    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=_("✏️ Изменить"), callback_data=f"foodai:edit:{meal_id}"),
                InlineKeyboardButton(text=_("🗑 Удалить"), callback_data=f"de:del:{meal_id}:{page}"),
            ],
            [InlineKeyboardButton(text=_("◀️ К списку блюд"), callback_data=f"de:l:{page}")],
        ]
    )

    # Analytics: Detail opened
    if analytics.logger and callback.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="DiaryEdit:DetailOpened",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                        text=f"meal_id={meal_id}, page={page}",
                        command=None,
                    ),
                    language=getattr(callback.from_user, 'language_code', None),
                )
            )
        except Exception:
            pass

    await _edit_caption_or_text(callback, text, kb=kb)
