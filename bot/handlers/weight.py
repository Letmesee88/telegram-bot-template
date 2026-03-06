from __future__ import annotations
import contextlib
from typing import TYPE_CHECKING

from aiogram import F, Router, types
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.i18n import gettext as _

from bot.analytics.types import BaseEvent, EventProperties
from bot.core.loader import redis_client
from bot.services.analytics import analytics
from bot.services.weight import (
    build_history_page,
    build_my_weight_text,
    save_weight,
)

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext

router = Router(name="weight")


class WeightRecord(StatesGroup):
    waiting_for_weight = State()


def _kb_my_weight() -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = []
    rows.append([
        types.InlineKeyboardButton(text=_("✏️ Записать вес"), callback_data="weight:record:start"),
        types.InlineKeyboardButton(text=_("📉 История моего веса"), callback_data="weight:history:1"),
    ])
    rows.append([types.InlineKeyboardButton(text=_("◀️ Вернуться назад"), callback_data="account:open:weight")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^weight:open(?::(.*))?$"))
async def cb_weight_open(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    text = await build_my_weight_text(user_id)
    try:
        await callback.message.answer(text, reply_markup=_kb_my_weight())
    except Exception:
        await callback.answer(_("⚖️ Мой вес"), show_alert=False)

    # Analytics
    if analytics.logger and callback.from_user:
        with contextlib.suppress(Exception):
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="WeightOpen",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                        command=None,
                    ),
                    language=getattr(callback.from_user, "language_code", None),
                )
            )


@router.callback_query(F.data == "weight:record:start")
async def cb_weight_record_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WeightRecord.waiting_for_weight)
    text = _("✏️ Записываю ваш вес\nВведи текущий вес в килограммах (пример: 60.5):")
    try:
        await callback.message.answer(text)
    except Exception:
        await callback.answer(text, show_alert=False)

    if analytics.logger and callback.from_user:
        with contextlib.suppress(Exception):
            analytics.fire_event(
                BaseEvent(
                    user_id=callback.from_user.id,
                    event_type="WeightRecordStart",
                    event_properties=EventProperties(
                        chat_id=callback.message.chat.id if callback.message else None,
                        chat_type=callback.message.chat.type if callback.message else None,
                    ),
                    language=getattr(callback.from_user, "language_code", None),
                )
            )


@router.message(WeightRecord.waiting_for_weight)
async def msg_weight_value(message: types.Message, state: FSMContext) -> None:
    # Only text is allowed. If not text → generic prompt
    if not message.text:
        await message.answer(_("Попробуйте еще раз (Пример: 60.5)"))
        if analytics.logger and message.from_user:
            with contextlib.suppress(Exception):
                analytics.fire_event(BaseEvent(
                    user_id=message.from_user.id,
                    event_type="WeightInputInvalid",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text="non_text",
                        command=None,
                    ),
                    language=getattr(message.from_user, "language_code", None),
                ))
        return

    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except Exception:
        await message.answer(_("Попробуйте еще раз (Пример: 60.5)"))
        if analytics.logger and message.from_user:
            with contextlib.suppress(Exception):
                analytics.fire_event(BaseEvent(
                    user_id=message.from_user.id,
                    event_type="WeightInputInvalid",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text=(raw[:32] if raw else None),
                        command=None,
                    ),
                    language=getattr(message.from_user, "language_code", None),
                ))
        return

    if value < 30 or value > 300:
        await message.answer(_("Пожалуйста, введи корректный вес (30–300 кг)"))
        if analytics.logger and message.from_user:
            with contextlib.suppress(Exception):
                analytics.fire_event(BaseEvent(
                    user_id=message.from_user.id,
                    event_type="WeightOutOfRange",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text=f"{value:.3f}",
                        command=None,
                    ),
                    language=getattr(message.from_user, "language_code", None),
                ))
        return

    value = round(value, 1)
    await state.update_data(value_kg=value)
    confirm_text = _("⚖️ Вес: {w} кг\nСохраняем запись?").format(w=f"{value:.1f}")
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text=_("✅ Да"), callback_data="weight:confirm:yes"),
                types.InlineKeyboardButton(text=_("❌ Нет"), callback_data="weight:confirm:no"),
            ]
        ]
    )
    await message.answer(confirm_text, reply_markup=kb)


@router.callback_query(F.data == "weight:confirm:yes")
async def cb_weight_confirm_yes(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    value = data.get("value_kg")
    if value is None:
        await callback.answer(_("Нет значения веса"), show_alert=True)
        return
    user_id = callback.from_user.id if callback.from_user else None
    if not user_id:
        return
    saved_value, local_date = await save_weight(user_id, float(value))
    # Invalidate account summary cache so new weight is reflected immediately
    with contextlib.suppress(Exception):
        await redis_client.delete(f"account:summary:{user_id}")
    await state.clear()

    date_str = local_date.strftime("%d.%m.%Y")
    text = _("✅ Новый вес записан!\n📅 Дата: {d}\n⚖️ Вес: {w} кг").format(d=date_str, w=f"{saved_value:.1f}")
    kb = _kb_my_weight()
    try:
        await callback.message.answer(text, reply_markup=kb)
    except Exception:
        await callback.answer(text, show_alert=False)

    if analytics.logger and callback.from_user:
        with contextlib.suppress(Exception):
            analytics.fire_event(BaseEvent(
                user_id=callback.from_user.id,
                event_type="WeightSaved",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"{saved_value:.1f} @ {date_str}",
                    command=None,
                ),
                language=getattr(callback.from_user, "language_code", None),
            ))


@router.callback_query(F.data == "weight:confirm:no")
async def cb_weight_confirm_no(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    # Back to My Weight screen
    if not callback.from_user:
        return
    text = await build_my_weight_text(callback.from_user.id)
    try:
        await callback.message.answer(text, reply_markup=_kb_my_weight())
    except Exception:
        await callback.answer(_("⚖️ Мой вес"), show_alert=False)

    if analytics.logger and callback.from_user:
        with contextlib.suppress(Exception):
            analytics.fire_event(BaseEvent(
                user_id=callback.from_user.id,
                event_type="WeightConfirmNo",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                ),
                language=getattr(callback.from_user, "language_code", None),
            ))


@router.callback_query(F.data.regexp(r"^weight:history:(\d+)$"))
async def cb_weight_history(callback: types.CallbackQuery) -> None:
    if not callback.from_user:
        return
    m = (callback.data or "").split(":")
    page = 1
    try:
        if len(m) >= 3:
            page = max(1, int(m[2]))
    except Exception:
        page = 1

    page_obj = await build_history_page(callback.from_user.id, page=page, page_size=20)
    rows: list[list[types.InlineKeyboardButton]] = []
    nav_row: list[types.InlineKeyboardButton] = []
    if page_obj.has_prev:
        nav_row.append(types.InlineKeyboardButton(text=_("◀️ Предыдущая страница"), callback_data=f"weight:history:{page_obj.page-1}"))
    if page_obj.has_next:
        nav_row.append(types.InlineKeyboardButton(text=_("Следующая страница ▶️"), callback_data=f"weight:history:{page_obj.page+1}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([types.InlineKeyboardButton(text=_("◀️ Вернуться назад"), callback_data="weight:open")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        await callback.message.answer(page_obj.text, reply_markup=kb)
    except Exception:
        await callback.answer(_("📉 История моего веса"), show_alert=False)

    if analytics.logger and callback.from_user:
        with contextlib.suppress(Exception):
            analytics.fire_event(BaseEvent(
                user_id=callback.from_user.id,
                event_type="WeightHistoryOpen",
                event_properties=EventProperties(
                    chat_id=callback.message.chat.id if callback.message else None,
                    chat_type=callback.message.chat.type if callback.message else None,
                    text=f"page={page_obj.page}",
                    command=None,
                ),
                language=getattr(callback.from_user, "language_code", None),
            ))
