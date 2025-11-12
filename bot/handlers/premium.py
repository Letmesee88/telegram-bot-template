from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F

from bot.services.yookassa import create_payment


router = Router(name="premium")


@router.message(Command("premium"))
async def premium_cmd(message: types.Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Trial", callback_data="buy:trial")],
            [InlineKeyboardButton(text="Month", callback_data="buy:month")],
            [InlineKeyboardButton(text="Year", callback_data="buy:year")],
        ]
    )
    await message.answer("Выбери план подписки:", reply_markup=kb)


@router.callback_query(F.data.startswith("buy:"))
async def buy_plan(call: types.CallbackQuery) -> None:
    await call.answer()
    plan = call.data.split(":", 1)[1] if call.data else ""
    user_id = call.from_user.id if call.from_user else None
    if not user_id or plan not in {"trial", "month", "year"}:
        return
    try:
        cp = await create_payment(user_id=user_id, plan=plan)
        await call.message.answer(f"Ссылка на оплату: {cp.confirmation_url}")
    except Exception:
        await call.message.answer("Ошибка при создании платежа. Попробуй позже.")
