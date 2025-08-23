from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel
from bot.schemas.onboarding import ActivityLevel, Gender, Goal, OnboardingData, Speed
from bot.services.plan import calculate_daily_plan, _infer_activity_level as infer_activity_level

router = Router()


class OnboardingStates(StatesGroup):
    gender = State()
    age = State()
    weight = State()
    height = State()
    activity = State()
    goal = State()
    speed = State()


@router.message(Command("onboarding"))
@router.message(Command("onbording"))  # alias for common typo
async def cmd_onboarding(message: Message, state: FSMContext) -> None:
    logger.info("/onboarding command received | from_user={} | chat_id={}", getattr(message.from_user, 'id', None), getattr(message.chat, 'id', None))
    await state.clear()
    await state.set_state(OnboardingStates.gender)
    await message.answer(_("Укажи пол: male | female"))


# Allow launching from inline menu button
@router.callback_query(F.data == "onboarding")
async def cb_onboarding(call: CallbackQuery, state: FSMContext) -> None:
    await cmd_onboarding(call.message, state)  # type: ignore[arg-type]
    await call.answer()


# Fallback in case Telegram didn't mark entity as bot_command
@router.message(F.text.casefold().startswith("/onboarding"))
@router.message(F.text.casefold().startswith("/onbording"))  # alias fallback
async def cmd_onboarding_fallback(message: Message, state: FSMContext) -> None:
    await cmd_onboarding(message, state)


@router.message(OnboardingStates.gender, F.text.casefold().in_(["male", "female"]))
async def gender_set(message: Message, state: FSMContext) -> None:
    await state.update_data(gender=message.text.strip().lower())
    await state.set_state(OnboardingStates.age)
    await message.answer(_("Возраст (целое число лет):"))


@router.message(OnboardingStates.gender)
async def gender_retry(message: Message) -> None:
    await message.answer(_("Введи male или female"))


@router.message(OnboardingStates.age, F.text.regexp(r"^\d{1,3}$"))
async def age_set(message: Message, state: FSMContext) -> None:
    await state.update_data(age=int(message.text))
    await state.set_state(OnboardingStates.weight)
    await message.answer(_("Вес, кг (например: 82.5):"))


@router.message(OnboardingStates.age)
async def age_retry(message: Message) -> None:
    await message.answer(_("Некорректный возраст. Пример: 27"))


@router.message(OnboardingStates.weight, F.text.regexp(r"^\d{2,3}(\.\d{1,2})?$"))
async def weight_set(message: Message, state: FSMContext) -> None:
    await state.update_data(weight_kg=float(message.text.replace(",", ".")))
    await state.set_state(OnboardingStates.height)
    await message.answer(_("Рост, см (например: 178):"))


@router.message(OnboardingStates.weight)
async def weight_retry(message: Message) -> None:
    await message.answer(_("Некорректный вес. Пример: 82.5"))


@router.message(OnboardingStates.height, F.text.regexp(r"^\d{3}$"))
async def height_set(message: Message, state: FSMContext) -> None:
    await state.update_data(height_cm=float(message.text))
    await state.set_state(OnboardingStates.activity)
    await message.answer(
        _(
            "Опиши активность в свободной форме (минимум 10 символов). Примеры: \n"
            "- 3 раза в неделю спортзал и бег\n- Сидячая работа, иногда прогулки"
        )
    )


@router.message(OnboardingStates.height)
async def height_retry(message: Message) -> None:
    await message.answer(_("Некорректный рост. Пример: 178"))


@router.message(OnboardingStates.activity, F.text.len() >= 10)
async def activity_set(message: Message, state: FSMContext) -> None:
    await state.update_data(activity_text=message.text.strip())
    await state.set_state(OnboardingStates.goal)
    await message.answer(_("Цель: lose | gain | maintain"))


@router.message(OnboardingStates.activity)
async def activity_retry(message: Message) -> None:
    await message.answer(_("Слишком коротко. Напиши подробнее (минимум 10 символов)"))


@router.message(OnboardingStates.goal, F.text.casefold().in_(["lose", "gain", "maintain"]))
async def goal_set(message: Message, state: FSMContext) -> None:
    await state.update_data(goal=message.text.strip().lower())
    await state.set_state(OnboardingStates.speed)
    await message.answer(_("Скорость: COMFORT | EFFORT | FAST (или пропусти сообщением '-')"))


@router.message(OnboardingStates.goal)
async def goal_retry(message: Message) -> None:
    await message.answer(_("Введи одну из целей: lose | gain | maintain"))


@router.message(OnboardingStates.speed)
async def speed_and_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()

    # optional speed
    speed_raw = message.text.strip().upper() if message.text else None
    speed = None
    if speed_raw and speed_raw != "-":
        if speed_raw not in {"COMFORT", "EFFORT", "FAST"}:
            await message.answer(_("Скорость не распознана. Используй: COMFORT | EFFORT | FAST | '-' для пропуска"))
            return
        speed = Speed(speed_raw)

    try:
        payload = OnboardingData(
            user_id=message.from_user.id,  # type: ignore[union-attr]
            gender=Gender(data["gender"]),
            age=int(data["age"]),
            weight_kg=float(data["weight_kg"]),
            height_cm=float(data["height_cm"]),
            activity_text=str(data["activity_text"]),
            goal=Goal(data["goal"]),
            speed=speed,
        )
    except Exception as e:  # pydantic validation errors
        logger.warning(f"Onboarding validation failed: {e}")
        await message.answer(_("Данные не прошли валидацию. Попробуй заново: /onboarding"))
        await state.clear()
        return

    # Determine and store explicit activity level in data JSON
    level: ActivityLevel = payload.activity_level or infer_activity_level(payload.activity_text)

    plan = calculate_daily_plan(payload)

    # persist into DB (upsert by user_id)
    try:
        async with sessionmaker() as session:
            existing = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == payload.user_id)
            )

            data_json = payload.model_dump()
            data_json["activity_level"] = level.value

            if existing:
                existing.data = data_json
                existing.daily_plan = plan.model_dump()
                existing.goal = payload.goal.value
                existing.calories = plan.calories
            else:
                record = OnboardingAnswerModel(
                    user_id=payload.user_id,
                    data=data_json,
                    daily_plan=plan.model_dump(),
                    goal=payload.goal.value,
                    calories=plan.calories,
                )
                session.add(record)
            await session.commit()
    except Exception as e:
        logger.exception("onboarding.speed.db_error | user_id={} | error={}", payload.user_id, e)
        await message.answer(_("Не удалось сохранить данные. Попробуй ещё раз или позже: /onboarding"))
        # оставляем состояние, чтобы пользователь мог повторить ввод скорости или отменить
        return

    await message.answer(
        _("Готово! Твой дневной план:")
        + "\n"
        + f"- {_("Калории")}: {plan.calories}\n"
        + f"- {_("Белки")}: {plan.protein_g} г\n"
        + f"- {_("Жиры")}: {plan.fat_g} г\n"
        + f"- {_("Углеводы")}: {plan.carbs_g} г\n"
        + "\n"
        + f"{_("Цель")}: {payload.goal.value} | {_("Скорость")}: {speed.value if speed else '-'} | {_("Активность")}: {level.value}"
    )

    await state.clear()
