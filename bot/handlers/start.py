from aiogram import Router, types
from aiogram.filters import CommandStart
from aiogram.utils.i18n import gettext as _
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F
from bot.services.analytics import analytics

router = Router(name="start")


@router.message(CommandStart())
@analytics.track_event("Sign Up")
async def start_handler(message: types.Message) -> None:
    """Welcome message."""
    text = (
        "Привет! 👋\n"
        "Я помогаю поддерживать фигуру с помощью контроля калорий и БЖУ.\n\n"
        "Процесс максимально простой:\n"
        "1. Определяем начальные показатели и цели\n"
        "2. Рассчитываем необходимое потребление калорий с балансом БЖУ\n"
        "3. Каждый день на основе фото или описания блюд считаем калории и, при необходимости, корректируем рацион\n\n"
        "Это гораздо удобнее, чем считать калории вручную, поэтому по статистике наши пользователи в 2 раза чаще достигают поставленных целей.\n\n"
        "У тебя уже есть план питания. Хочешь его сбросить и построить новый?"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Да", callback_data="onboarding_start"),
            InlineKeyboardButton(text="Нет", callback_data="start:no"),
        ]]
    )

    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "start:no")
async def start_no(call: types.CallbackQuery) -> None:
    # Пока ничего не делаем, просто закрываем спиннер
    await call.answer()
