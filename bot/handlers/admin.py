from __future__ import annotations

from time import perf_counter

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.loader import redis_client
from bot.filters.admin import AdminFilter
from bot.services.users import get_user_count, get_all_users


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


@router.message(Command(commands=["broadcast"]), AdminFilter())
async def broadcast_start(message: types.Message, state: FSMContext) -> None:
    """Start broadcast workflow: ask admin to provide text."""
    await state.clear()
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer(
        "Пришлите текст рассылки одним сообщением. HTML разрешён. После этого я попрошу подтверждение."
    )


@router.message(BroadcastStates.waiting_text, AdminFilter())
async def broadcast_preview(message: types.Message, state: FSMContext, session: AsyncSession) -> None:
    """Store text and show preview with Confirm/Cancel."""
    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("Текст пуст. Пришлите непустое сообщение.")
        return

    await state.update_data(text=text)
    users_total = await get_user_count(session)

    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить", callback_data="broadcast:send")
    kb.button(text="Отмена", callback_data="broadcast:cancel")
    kb.adjust(2)

    await message.answer(
        f"Предпросмотр рассылки ({users_total} пользователей):\n\n{text}",
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "broadcast:cancel", AdminFilter())
async def broadcast_cancel(call: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    await call.message.answer("Рассылка отменена.")


@router.callback_query(F.data == "broadcast:send", AdminFilter())
async def broadcast_send(call: types.CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Send the stored text to all users from DB."""
    data = await state.get_data()
    text = data.get("text")
    if not text:
        await call.answer("Нет текста для отправки", show_alert=True)
        return

    await call.answer()
    await call.message.answer("Начинаю рассылку...")

    users = await get_all_users(session)
    sent = 0
    skipped = 0

    for user in users:
        try:
            await call.bot.send_message(user.id, text, disable_web_page_preview=True)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            skipped += 1
        except Exception:
            skipped += 1

    await state.clear()
    await call.message.answer(f"Готово. Отправлено: {sent}. Пропущено: {skipped}.")
