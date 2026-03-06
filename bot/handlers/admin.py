from __future__ import annotations
import asyncio
from time import perf_counter
from typing import TYPE_CHECKING

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import exists, func, select

from bot.core.loader import redis_client
from bot.database.models import PaymentModel, UserModel
from bot.filters.admin import AdminFilter
from bot.services.users import get_user_count

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from sqlalchemy.ext.asyncio import AsyncSession

router = Router(name="admin")


@router.message(Command(commands=["ping"]), AdminFilter())
async def ping_handler(message: types.Message, session: AsyncSession) -> None:
    """Healthcheck for admins: reports latency and checks DB/Redis connectivity."""
    t0 = perf_counter()

    # DB check
    db_ok = True
    try:
        await session.execute(select(1))
    except Exception:
        db_ok = False

    # Redis check
    redis_ok = True
    try:
        pong = await redis_client.ping()
        redis_ok = bool(pong)
    except Exception:
        redis_ok = False

    latency_ms = int((perf_counter() - t0) * 1000)
    status = "OK" if (db_ok and redis_ok) else "DEGRADED"

    text = (
        f"pong: {status}\n"
        f"latency: {latency_ms} ms\n"
        f"DB: {'OK' if db_ok else 'FAIL'}\n"
        f"Redis: {'OK' if redis_ok else 'FAIL'}"
    )
    await message.answer(text)


@router.message(Command(commands=["stats"]), AdminFilter())
async def stats_handler(message: types.Message, session: AsyncSession) -> None:
    """Basic stats for admins."""
    users_total = await get_user_count(session)

    text = (
        "📊 Stats\n"
        f"Users: {users_total}\n"
    )
    await message.answer(text)


# =========================
# Broadcast with confirmation
# =========================


class BroadcastStates(StatesGroup):
    waiting_text = State()
    waiting_subscribe_btn_text = State()
    waiting_group_btn_text = State()
    waiting_group_url = State()
    waiting_confirm = State()


def _audience_predicate(segment: str):
    paid_exists = exists(
        select(PaymentModel.id).where(
            PaymentModel.user_id == UserModel.id,
            PaymentModel.status == "succeeded",
        )
    )
    if segment == "paid":
        return paid_exists
    if segment == "free":
        return ~paid_exists
    return None


async def _audience_count(session: AsyncSession, segment: str) -> int:
    pred = _audience_predicate(segment)
    stmt = select(func.count()).select_from(UserModel)
    if pred is not None:
        stmt = stmt.where(pred)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _iter_audience_user_ids(session: AsyncSession, segment: str):
    pred = _audience_predicate(segment)
    stmt = select(UserModel.id)
    if pred is not None:
        stmt = stmt.where(pred)
    stream = await session.stream_scalars(stmt)
    async for uid in stream:
        try:
            yield int(uid)
        except Exception:
            continue


def _broadcast_kb(subscribe_text: str | None, group_text: str | None, group_url: str | None) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if subscribe_text:
        rows.append([InlineKeyboardButton(text=subscribe_text, callback_data="sale:choose")])
    if group_text and group_url:
        rows.append([InlineKeyboardButton(text=group_text, url=group_url)])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command(commands=["broadcast"]), AdminFilter())
async def broadcast_start(message: types.Message, state: FSMContext) -> None:
    """Start broadcast workflow: ask admin to provide text."""
    await state.clear()
    await state.update_data(segment="all")
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer(
        "Пришлите текст рассылки одним сообщением. HTML разрешён. После этого я попрошу настройки кнопок и подтверждение."
    )


@router.message(Command(commands=["broadcast_paid"]), AdminFilter())
async def broadcast_start_paid(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(segment="paid")
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer(
        "Пришлите текст рассылки (только тем, кто когда-либо покупал). HTML разрешён. "
        "После этого я попрошу настройки кнопок и подтверждение."
    )


@router.message(Command(commands=["broadcast_free"]), AdminFilter())
async def broadcast_start_free(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(segment="free")
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer(
        "Пришлите текст рассылки (только тем, кто ещё ни разу не покупал). HTML разрешён. "
        "После этого я попрошу настройки кнопок и подтверждение."
    )


@router.message(BroadcastStates.waiting_text, AdminFilter())
async def broadcast_preview(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Store text and show preview with Confirm/Cancel."""
    raw_text = (message.text or "").strip()
    text = (message.html_text or raw_text).strip() if message.entities else raw_text
    if not text:
        await message.answer("Текст пуст. Пришлите непустое сообщение.")
        return

    await state.update_data(text=text)
    await state.set_state(BroadcastStates.waiting_subscribe_btn_text)
    await message.answer(
        "Текст кнопки подписки (callback: sale:choose).\n"
        "По умолчанию: 💎 Выбрать тариф\n\n"
        "Пришлите новый текст, '-' чтобы оставить по умолчанию, или 'нет' чтобы не добавлять эту кнопку."
    )


@router.message(BroadcastStates.waiting_subscribe_btn_text, AdminFilter())
async def broadcast_set_subscribe_btn_text(message: types.Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"нет", "no", "none"}:
        subscribe_text = None
    elif raw and raw != "-":
        subscribe_text = raw
    else:
        subscribe_text = "💎 Выбрать тариф"
    await state.update_data(subscribe_text=subscribe_text)
    await state.set_state(BroadcastStates.waiting_group_btn_text)
    await message.answer(
        "Текст кнопки группы (URL).\n"
        "По умолчанию: 👥 Вступить в группу\n\n"
        "Пришлите новый текст, '-' чтобы оставить по умолчанию, или 'нет' чтобы не добавлять кнопку группы."
    )


@router.message(BroadcastStates.waiting_group_btn_text, AdminFilter())
async def broadcast_set_group_btn_text(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"нет", "no", "none"}:
        await state.update_data(group_text=None, group_url=None)
        await _render_broadcast_preview(message, state, session)
        return
    group_text = raw if raw and raw != "-" else "👥 Вступить в группу"
    await state.update_data(group_text=group_text)
    await state.set_state(BroadcastStates.waiting_group_url)
    await message.answer(
        "Ссылка на группу (пример: https://t.me/your_group).\n"
        "Пришлите URL, или '-' чтобы не добавлять кнопку группы."
    )


async def _render_broadcast_preview(
    message: types.Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    text = str(data.get("text") or "").strip()
    segment = str(data.get("segment") or "all").strip()
    subscribe_text = data.get("subscribe_text")  # may be None
    group_text = data.get("group_text")
    group_url = data.get("group_url")
    cnt = await _audience_count(session, segment)
    seg_label = {"all": "всем", "paid": "покупавшим", "free": "не покупавшим"}.get(segment, segment)

    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить", callback_data="broadcast:send")
    kb.button(text="Отмена", callback_data="broadcast:cancel")
    kb.adjust(2)

    preview_kb = _broadcast_kb(subscribe_text, group_text, group_url)

    try:
        await message.answer(
            f"Предпросмотр рассылки ({cnt} пользователей, сегмент: {seg_label}):\n\n{text}",
            reply_markup=kb.as_markup(),
            disable_web_page_preview=True,
        )
        if preview_kb:
            await message.answer(
                "Кнопки:",
                reply_markup=preview_kb,
                disable_web_page_preview=True,
            )
        else:
            await message.answer("Без кнопок.")
    except TelegramBadRequest:
        await state.update_data(text=None)
        await state.set_state(BroadcastStates.waiting_text)
        await message.answer(
            "Не смог показать предпросмотр: похоже, в тексте невалидный HTML. Пришлите текст заново одним сообщением."
        )
        return

    await state.set_state(BroadcastStates.waiting_confirm)


@router.message(BroadcastStates.waiting_group_url, AdminFilter())
async def broadcast_set_group_url(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if raw and raw != "-":
        await state.update_data(group_url=raw)
    else:
        await state.update_data(group_text=None, group_url=None)

    await _render_broadcast_preview(message, state, session)


@router.message(BroadcastStates.waiting_confirm, AdminFilter())
async def broadcast_waiting_confirm(message: types.Message) -> None:
    await message.answer("Для продолжения нажмите кнопку 'Отправить' или 'Отмена' в предпросмотре.")


@router.callback_query(F.data == "broadcast:cancel", AdminFilter())
async def broadcast_cancel(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    await call.message.answer("Рассылка отменена.")


@router.callback_query(F.data == "broadcast:send", AdminFilter())
async def broadcast_send(call: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Send the stored text to all users from DB."""
    data = await state.get_data()
    text = str(data.get("text") or "").strip()
    segment = str(data.get("segment") or "all").strip()
    subscribe_text = data.get("subscribe_text")  # may be None
    group_text = data.get("group_text")
    group_url = data.get("group_url")
    if not text:
        await call.answer("Нет текста для отправки", show_alert=True)
        return

    await call.answer()
    await call.message.answer("Начинаю рассылку...")

    sent = 0
    skipped = 0

    kb_send = _broadcast_kb(subscribe_text, group_text, group_url)  # may be None
    base_delay_sec = 0.05

    async def _send_with_retry(chat_id: int) -> bool:
        while True:
            try:
                await call.bot.send_message(chat_id, text, disable_web_page_preview=True, reply_markup=kb_send)
                return True
            except TelegramRetryAfter as e:
                retry_after = float(getattr(e, "retry_after", 1))
                await asyncio.sleep(max(0.0, retry_after) + 0.5)
            except (TelegramForbiddenError, TelegramBadRequest):
                return False
            except Exception:
                return False

    async for uid in _iter_audience_user_ids(session, segment):
        ok = await _send_with_retry(uid)
        if ok:
            sent += 1
        else:
            skipped += 1
        await asyncio.sleep(base_delay_sec)

    await state.clear()
    await call.message.answer(f"Готово. Отправлено: {sent}. Пропущено: {skipped}.")
