from aiogram import Router, types
from aiogram.filters import CommandStart
from aiogram.utils.i18n import gettext as _
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import F
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from bot.services.analytics import analytics
from bot.database.database import sessionmaker
from bot.database.models.onboarding_answer import OnboardingAnswerModel
from bot.analytics.types import BaseEvent, EventProperties, Plan

router = Router(name="start")


@router.message(CommandStart())
@analytics.track_event("Sign Up")
async def start_handler(message: types.Message, state: FSMContext) -> None:
    """Start screen with branching: completed / in_progress / fresh."""
    user_id = message.from_user.id if message.from_user else None

    # Intro text unified with /onboarding
    intro = _(
        "Привет! 👋\n"
        "Я помогаю поддерживать фигуру с помощью контроля калорий и БЖУ.\n\n"
        "Процесс максимально простой:\n"
        "1. Определяем начальные показатели и цели\n"
        "2. Рассчитываем необходимое потребление калорий с балансом БЖУ\n"
        "3. Каждый день на основе фото или описания блюд считаем калории и, при необходимости, корректируем рацион\n\n"
        "Это гораздо удобнее, чем считать калории вручную, поэтому по статистике наши пользователи в 2 раза чаще достигают поставленных целей.\n\n"
    )

    completed = False
    if user_id is not None:
        async with sessionmaker() as session:
            exists = await session.scalar(
                select(OnboardingAnswerModel.id).where(OnboardingAnswerModel.user_id == user_id)
            )
            completed = bool(exists)

    current_state = await state.get_state()
    in_progress = current_state is not None

    if completed:
        text = intro + _("У тебя уже есть план питания. Хочешь его сбросить и построить новый?")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text=_("Да"), callback_data="onboarding_start"),
                InlineKeyboardButton(text=_("Нет"), callback_data="start:no"),
            ]]
        )
        # Analytics: log start type as Completed
        if analytics.logger and user_id is not None:
            await analytics.logger.log_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Start Session",
                    event_properties=EventProperties(
                        chat_id=message.chat.id if message.chat else None,
                        chat_type=message.chat.type if message.chat else None,
                        text=None,
                        command="/start",
                    ),
                    language=message.from_user.language_code if message.from_user else None,
                    plan=Plan(branch="Completed", source="start", version="v1"),
                )
            )
        await message.answer(text, reply_markup=kb)
        return

    # not completed: branch by in-progress vs fresh
    if in_progress:
        text = intro + _("Ты уже начал онбординг. Продолжим с места, где остановились?")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text=_("Продолжить"), callback_data="onboarding_resume"),
                InlineKeyboardButton(text=_("Начать заново"), callback_data="onboarding_restart"),
            ]]
        )
    else:
        text = intro + _("Приступим? 🚀")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=_("Начнем"), callback_data="onboarding_start")]]
        )
    # Analytics: log Fresh vs InProgress start type
    if analytics.logger and user_id is not None:
        await analytics.logger.log_event(
            BaseEvent(
                user_id=user_id,
                event_type="Start Session",
                event_properties=EventProperties(
                    chat_id=message.chat.id if message.chat else None,
                    chat_type=message.chat.type if message.chat else None,
                    text=None,
                    command="/start",
                ),
                language=message.from_user.language_code if message.from_user else None,
                plan=Plan(branch=("InProgress" if in_progress else "Fresh"), source="start", version="v1"),
            )
        )
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "start:no")
async def start_no(call: types.CallbackQuery) -> None:
    # Явный короткий ответ, чтобы не было тишины
    await call.answer()
    await call.message.answer(
        _("Ок, оставляем текущий план. Готов считать калории — пришли фото блюда или описание.")
    )
