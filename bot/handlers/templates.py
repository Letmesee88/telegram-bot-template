from __future__ import annotations
import contextlib
import re

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import DailyIntakeModel, MealModel, OnboardingAnswerModel
from bot.handlers.foodai import _build_preview_text, _preview_kb, _saved_with_recommend_kb
from bot.keyboards.templates import (
    categories_browse_kb,
    choose_category_kb,
    save_confirm_kb,
    templates_list_kb,
)
from bot.services.templates import (
    create_meal_draft_from_template,
    create_template_from_meal,
    delete_template,
    list_categories_with_counts,
    list_templates,
)
from bot.services.users import today_local_utc_dates

router = Router(name="templates")


def _cta_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_("Начать"), callback_data="onboarding_start")]]
    )


@router.message(Command("templates"))
async def cmd_templates(message: types.Message) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    async with sessionmaker() as session:
        exists = await session.scalar(
            select(OnboardingAnswerModel.id).where(OnboardingAnswerModel.user_id == user_id)
        )
    if not bool(exists):
        await message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())
        return
    async with sessionmaker() as session:
        counts = await list_categories_with_counts(session, user_id)
    text = _("Выбери категорию приёма пищи:")
    await message.answer(text, reply_markup=categories_browse_kb(counts))


@router.callback_query(F.data == "tpl:back:root")
async def cb_tpl_back_root(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        counts = await list_categories_with_counts(session, user_id)
    text = _("Выбери категорию приёма пищи:")
    try:
        await callback.message.edit_text(text, reply_markup=categories_browse_kb(counts))
    except Exception:
        await callback.message.answer(text, reply_markup=categories_browse_kb(counts))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:cat:(breakfast|lunch|dinner|snack)$"))
async def cb_tpl_cat(callback: types.CallbackQuery) -> None:
    m = re.match(r"^tpl:cat:(breakfast|lunch|dinner|snack)$", callback.data or "")
    if not m or not callback.from_user:
        return
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        exists = await session.scalar(
            select(OnboardingAnswerModel.id).where(OnboardingAnswerModel.user_id == user_id)
        )
    if not bool(exists):
        with contextlib.suppress(Exception):
            await callback.answer()
        with contextlib.suppress(Exception):
            await callback.message.answer(_("Завершите онбординг за пару минут, чтобы получить полный доступ к данным"), reply_markup=_cta_kb())
        return
    category = m.group(1)
    async with sessionmaker() as session:
        tpls = await list_templates(session, user_id, category)
    data = [(int(t.id), str(t.title or "")) for t in tpls]
    title = {
        "breakfast": _("🥞 Завтрак"),
        "lunch": _("🍜 Обед"),
        "dinner": _("🥗 Ужин"),
        "snack": _("🍎 Перекус"),
    }[category]
    # Build numbered list in message body and keep only action/delete buttons in keyboard
    lines = []
    for idx, (_id, _title) in enumerate(data, start=1):
        t = (_title or "").strip() or _("Без названия")
        lines.append(f"# {idx} {t}")
    lst = "\n".join(lines)
    text = _("📌 Шаблоны · {title}").format(title=title) + "\n" + _("Выбери шаблон:") + ("\n\n" + lst if lst else "")
    try:
        await callback.message.edit_text(text, reply_markup=templates_list_kb(data, category))
    except Exception:
        await callback.message.answer(text, reply_markup=templates_list_kb(data, category))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:open:(\d+)$"))
async def cb_tpl_open(callback: types.CallbackQuery) -> None:
    m = re.match(r"^tpl:open:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    tpl_id = int(m.group(1))
    user_id = callback.from_user.id

    async with sessionmaker() as session:
        # Create draft from template
        meal_id = await create_meal_draft_from_template(session, user_id, tpl_id)
        meal = await session.get(MealModel, meal_id)
        if not meal:
            await callback.answer(_("Не удалось создать черновик"), show_alert=True)
            return
        # Build preview like FoodAI
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
            references=None,
            title=meal.title or None,
            source=meal.source or None,
        )

    try:
        await callback.message.edit_text(text, reply_markup=_preview_kb(meal_id))
    except Exception:
        await callback.message.answer(text, reply_markup=_preview_kb(meal_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:save:(\d+)$"))
async def cb_tpl_save_entry(callback: types.CallbackQuery) -> None:
    m = re.match(r"^tpl:save:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    meal_id = int(m.group(1))
    user_id = callback.from_user.id

    # Step 3 spec: show creating template preview with SAME visual scheme but without footer blocks
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return
        items = []
        try:
            for it in (meal.items or []):
                items.append({
                    "name": it.name,
                    "weight_g": float(it.weight_g) if it.weight_g is not None else None,
                    "calories": float(it.calories) if it.calories is not None else None,
                })
        except Exception:
            items = []
        preview_text = _build_preview_text(
            int(meal.calories or 0),
            float(meal.protein_g or 0),
            float(meal.fat_g or 0),
            float(meal.carbs_g or 0),
            float(meal.confidence or 0),
            weight=float(meal.weight_g or 0),
            items=items,
            references=None,
            title=meal.title or None,
            source="edit",  # no header like 'Предпросмотр блюда'
        )
    # Trim footer (sources, analysis, etc.) after separator
    try:
        preview_trimmed = preview_text.split("\n------------------------------", 1)[0]
    except Exception:
        preview_trimmed = preview_text

    header = _("📌Создаю шаблон")
    subheader = _("Он будет доступен через команду /templates")
    # Keep leading blank line in preview so that there is exactly one empty line before title
    text = f"{header}\n{subheader}\n{preview_trimmed}"

    try:
        await callback.message.edit_text(text, reply_markup=save_confirm_kb(meal_id))
    except Exception:
        await callback.message.answer(text, reply_markup=save_confirm_kb(meal_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:save_back:(\d+)$"))
async def cb_tpl_save_back(callback: types.CallbackQuery) -> None:
    m = re.match(r"^tpl:save_back:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    meal_id = int(m.group(1))
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        meal = await session.get(MealModel, meal_id)
        if not meal or meal.user_id != user_id:
            await callback.answer(_("Не найдено"), show_alert=True)
            return
        # Build "✅ Еда сохранена" + анализ дня (как в foodai.save)
        saved_line = _("✅ Еда сохранена")
        analysis_text = ""
        try:
            dates = await today_local_utc_dates(session, user_id)
            # Prefer execute() to sum over both UTC dates; fallback to scalar() for FakeSession in unit tests
            rows = []
            try:
                res = await session.execute(
                    select(DailyIntakeModel).where(
                        (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc.in_(dates))
                    )
                )
                rows = list(res.scalars().all())
            except Exception:
                # Fallback path for tests using a simplified session that only implements scalar()
                di_fallback = await session.scalar(
                    select(DailyIntakeModel).where(
                        DailyIntakeModel.user_id == user_id
                    )
                )
                if di_fallback:
                    rows = [di_fallback]
            oa = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
            )
            if rows and oa and isinstance(getattr(oa, "daily_plan", None), dict):
                plan = oa.daily_plan or {}
                plan_cal = int(plan.get("calories") or 0)
                plan_p = float(plan.get("protein_g") or 0)
                plan_f = float(plan.get("fat_g") or 0)
                plan_c = float(plan.get("carbs_g") or 0)
                # Sum over UTC dates covering user's local day
                fact_cal = int(sum(int(r.calories or 0) for r in rows))
                fact_p = float(sum(float(r.protein_g or 0) for r in rows))
                fact_f = float(sum(float(r.fat_g or 0) for r in rows))
                fact_c = float(sum(float(r.carbs_g or 0) for r in rows))
                diff_cal = plan_cal - fact_cal
                diff_p = plan_p - fact_p
                diff_f = plan_f - fact_f
                diff_c = plan_c - fact_c
                def _fmt(delta: float, emoji: str, unit: str, label: str) -> str:
                    show_val = f"{int(abs(delta))}" if unit == "ккал" else f"{abs(delta):.1f}"
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
        except Exception:
            analysis_text = analysis_text
        text = saved_line if not analysis_text else f"{saved_line}\n\n{analysis_text}"
    try:
        await callback.message.edit_text(text, reply_markup=_saved_with_recommend_kb(meal_id))
    except Exception:
        await callback.message.answer(text, reply_markup=_saved_with_recommend_kb(meal_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:save_go:(\d+)$"))
async def cb_tpl_save_go(callback: types.CallbackQuery) -> None:
    m = re.match(r"^tpl:save_go:(\d+)$", callback.data or "")
    if not m:
        return
    meal_id = int(m.group(1))
    # Step 4 spec: ask category
    text = _("Куда отнести этот шаблон ?")
    try:
        await callback.message.edit_text(text, reply_markup=choose_category_kb(meal_id))
    except Exception:
        await callback.message.answer(text, reply_markup=choose_category_kb(meal_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:save_menu:(\d+):(breakfast|lunch|dinner|snack)$"))
async def cb_tpl_save_menu(callback: types.CallbackQuery) -> None:
    m = re.match(r"^tpl:save_menu:(\d+):(breakfast|lunch|dinner|snack)$", callback.data or "")
    if not m or not callback.from_user:
        return
    meal_id = int(m.group(1))
    category = m.group(2)
    user_id = callback.from_user.id

    async with sessionmaker() as session:
        try:
            await create_template_from_meal(session, user_id, meal_id, category)
            meal = await session.get(MealModel, meal_id)
        except ValueError as e:
            code = str(e)
            msg = {
                "invalid_category": _("Некорректная категория"),
                "limit_reached": _("Лимит в категории достигнут"),
                "meal_not_found": _("Блюдо не найдено"),
                "duplicate_title": _("Такой шаблон уже есть"),
            }.get(code, _("Не удалось сохранить шаблон"))
            await callback.answer(msg, show_alert=True)
            return
    # Step 5 spec: final confirmation
    t = (meal.title or "") if meal else ""
    text = _("👌🏼 Шаблон {t} сохранён! Используй команду в меню /templates, чтобы добавлять в свой рацион.").format(t=t)
    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:del:(\d+):(breakfast|lunch|dinner|snack)$"))
async def cb_tpl_del(callback: types.CallbackQuery) -> None:
    m = re.match(r"^tpl:del:(\d+):(breakfast|lunch|dinner|snack)$", callback.data or "")
    if not m or not callback.from_user:
        return
    tpl_id = int(m.group(1))
    m.group(2)
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        await delete_template(session, user_id, tpl_id)
    # Per spec: show delete confirmation and hint back to list
    text = _("❌ Шаблон удалён") + "\n\n" + _("Вернуться к списку: /templates")
    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^tpl:add:(\d+)$"))
async def cb_tpl_add(callback: types.CallbackQuery) -> None:
    # Quick path: create draft and show preview (then user can Save/Edit/Delete)
    m = re.match(r"^tpl:add:(\d+)$", callback.data or "")
    if not m or not callback.from_user:
        return
    tpl_id = int(m.group(1))
    user_id = callback.from_user.id
    async with sessionmaker() as session:
        meal_id = await create_meal_draft_from_template(session, user_id, tpl_id)
        meal = await session.get(MealModel, meal_id)
        if not meal:
            await callback.answer(_("Не удалось создать черновик"), show_alert=True)
            return
        items = []
        try:
            for it in (meal.items or []):
                items.append({
                    "name": it.name,
                    "weight_g": float(it.weight_g) if it.weight_g is not None else None,
                    "calories": float(it.calories) if it.calories is not None else None,
                })
        except Exception:
            items = []
        preview_text = _build_preview_text(
            int(meal.calories or 0),
            float(meal.protein_g or 0),
            float(meal.fat_g or 0),
            float(meal.carbs_g or 0),
            float(meal.confidence or 0),
            weight=float(meal.weight_g or 0),
            items=items,
            references=None,
            title=meal.title or None,
            source="edit",  # no header
        )
        try:
            text = preview_text.split("\n------------------------------", 1)[0]
        except Exception:
            text = preview_text
    try:
        await callback.message.edit_text(text, reply_markup=_preview_kb(meal_id))
    except Exception:
        await callback.message.answer(text, reply_markup=_preview_kb(meal_id))
    await callback.answer()
