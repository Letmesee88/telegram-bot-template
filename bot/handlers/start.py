from aiogram import Router, types
import os
from aiogram.filters import CommandStart, Command
from aiogram.utils.i18n import gettext as _
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram import F
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from bot.services.analytics import analytics
from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models.onboarding_answer import OnboardingAnswerModel
from bot.analytics.types import BaseEvent, EventProperties, Plan

router = Router(name="start")


@router.message(CommandStart())
@analytics.track_event("Sign Up")
async def start_handler(message: types.Message, state: FSMContext) -> None:
    """Start screen with branching: completed / in_progress / fresh."""
    user_id = message.from_user.id if message.from_user else None

    # Force-reset FSM to avoid stale states (e.g., lingering OnboardingStates.adjust)
    try:
        await state.clear()
    except Exception:
        pass

    # Intro text unified with /onboarding
    intro = _(
        "Привет! 👋\n"
        "Я помогу скинуть лишний вес, сохраняя ваш обычный ритм жизни\n\n"
        "Как это работает:\n"
        "1. За 2 минуты определяем твои цели и рассчитываем дневную норму\n"
        "2. Получаешь персональный план питания\n"
        "3. Просто фотографируешь еду — я всё считаю за тебя\n\n"
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
        # Analytics: log start type as Completed (sync only when flag is set)
        if analytics.logger and user_id is not None:
            evt = BaseEvent(
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
            if settings.ANALYTICS_SYNC_START or os.getenv("PYTEST_CURRENT_TEST"):
                try:
                    await analytics.logger.log_event(evt)  # type: ignore[union-attr]
                except Exception:
                    pass
            else:
                analytics.fire_event(evt)
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
    # Analytics: log Fresh vs InProgress start type (sync only when flag is set)
    if analytics.logger and user_id is not None:
        evt = BaseEvent(
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
        if settings.ANALYTICS_SYNC_START or os.getenv("PYTEST_CURRENT_TEST"):
            try:
                await analytics.logger.log_event(evt)  # type: ignore[union-attr]
            except Exception:
                pass
        else:
            analytics.fire_event(evt)
    # Fresh: show intro video + caption (Telegram caption limit is ~1024 chars)
    if (not completed) and (not in_progress):
        try:
            video = FSInputFile("bot/static/intro.mp4")
            if len(text) <= 1024:
                await message.answer_video(video=video, caption=text, reply_markup=kb)
            else:
                await message.answer_video(video=video, caption=_("Приступим? 🚀"), reply_markup=kb)
                await message.answer(text)
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "start:no")
async def start_no(call: types.CallbackQuery) -> None:
    # Явный короткий ответ, чтобы не было тишины
    await call.answer()
    await call.message.answer(
        _("Ок, оставляем текущий план. Готов считать калории — пришли фото блюда или описание.")
    )


@router.message(Command("add_meal"))
async def cmd_add_meal(message: types.Message) -> None:
    text = (
        "➕ Как добавить блюдо:\n\n"
        "📸 Сфотографируйте блюдо\n"
        "✏️ Или опишите его словами\n\n"
        "📌 Для точного анализа:\n"
        "• Снимайте всю порцию целиком\n"
        "• Уточняйте размер порций и вес\n\n"
        "⚡️ Полезные функции:\n"
        "• Корректируйте данные после анализа\n"
        "• Сохраняйте любимые блюда как шаблоны\n\n"
        "calorissimo_ai_bot считает калории за вас и помогает добиваться результата !"
    )
    await message.answer(text)


@router.message(Command("support"))
async def cmd_support(message: types.Message) -> None:
    text = (
        "☕️ Поддержка  пользователей\n\n"
        "По любым вопросам пишите:\n"
        "@Tonya_19_93"
    )
    await message.answer(text, disable_web_page_preview=True)
