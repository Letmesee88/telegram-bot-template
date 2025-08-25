from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel
from bot.schemas.onboarding import ActivityLevel, Gender, Goal, OnboardingData, Speed
from bot.services.plan import (
    calculate_daily_plan,
    _infer_activity_level as infer_activity_level,
    SPEED_PERCENT_BY_WEIGHT,
)

router = Router()


class OnboardingStates(StatesGroup):
    gender = State()
    age = State()
    weight = State()
    height = State()
    activity = State()
    goal = State()
    goal_weight = State()
    speed = State()
    review = State()
    adjust = State()


# =====================
# Helpers
# =====================

def _ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_(text), callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def _format_rate(weight: float, percent: float) -> str:
    val = round(weight * percent, 2)
    # Приведем к удобному виду: 0.5, 0.75 и т.п.
    return ("{:.2f}".format(val)).rstrip('0').rstrip('.')


async def _finalize_and_show(message: Message, state: FSMContext, user_id: int) -> None:
    """Собирает payload, считает план, сохраняет в БД и показывает финальный экран с кнопками.
    Убираем TDEE из пользовательского вывода согласно ТЗ.
    """
    data = await state.get_data()

    speed_val = data.get("speed")
    speed = Speed(speed_val) if isinstance(speed_val, str) and speed_val else None

    try:
        payload = OnboardingData(
            user_id=user_id,  
            gender=Gender(str(data["gender"])),
            age=int(data["age"]),
            weight_kg=float(data["weight_kg"]),
            height_cm=float(data["height_cm"]),
            activity_text=str(data["activity_text"]),
            goal=Goal(str(data["goal"])),
            speed=speed,
            goal_weight_kg=float(data["goal_weight_kg"]) if data.get("goal_weight_kg") is not None else None,
        )
    except Exception as e:
        logger.warning(f"Onboarding validation failed: {e}")
        await message.answer(_("Данные не прошли валидацию. Попробуй заново: /onboarding"))
        await state.clear()
        return

    # Определим уровень активности, сохраним в data_json
    level: ActivityLevel = payload.activity_level or infer_activity_level(payload.activity_text)

    plan = calculate_daily_plan(payload)

    # Сохранение в БД (upsert)
    try:
        async with sessionmaker() as session:
            existing = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == payload.user_id)
            )

            data_json = payload.model_dump(mode="json")
            data_json["activity_level"] = level.value

            if existing:
                existing.data = data_json
                existing.daily_plan = plan.model_dump(mode="json")
                existing.goal = payload.goal.value
                existing.calories = plan.calories
            else:
                record = OnboardingAnswerModel(
                    user_id=payload.user_id,
                    data=data_json,
                    daily_plan=plan.model_dump(mode="json"),
                    goal=payload.goal.value,
                    calories=plan.calories,
                )
                session.add(record)
            await session.commit()
    except Exception as e:
        logger.exception("onboarding.finalize.db_error | user_id={} | error={}", payload.user_id, e)
        await message.answer(_("Не удалось сохранить данные. Попробуй ещё раз или позже: /onboarding"))
        return

    # Сформировать финальный текст согласно ТЗ
    lines: list[str] = []
    lines.append(_("Твой индивидуальный план готов!"))

    if payload.goal != Goal.maintain:
        # ETA и скорость
        if plan.eta_date is not None and payload.goal_weight_kg is not None:
            delta = abs(payload.weight_kg - payload.goal_weight_kg)
            formatted_date = plan.eta_date.strftime('%d.%m.%Y')
            if payload.goal == Goal.lose:
                lines.append(f"Ты сбросишь {round(delta, 1)} кг к {formatted_date}")
            elif payload.goal == Goal.gain:
                lines.append(f"Ты наберешь {round(delta, 1)} кг к {formatted_date}")
        lines.append(f"{_('Скорость')}: {plan.weekly_rate_kg} {_('кг в неделю')}")

    lines.append("")
    lines.append("<b>" + _("Дневная норма:") + "</b>")
    lines.append(f"🔥 {_('Калории')}: {plan.calories} {_('ккал')}")
    lines.append(f"🥩 {_('Белки')}: {plan.protein_g} {_('г')}")
    lines.append(f"🥑 {_('Жиры')}: {plan.fat_g} {_('г')}")
    lines.append(f"🍞 {_('Углеводы')}: {plan.carbs_g} {_('г')}")

    lines.append("")
    lines.append("📚 <b>" + _("Научные основы расчетов:") + "</b>")
    lines.append("• <a href=\"https://pubmed.ncbi.nlm.nih.gov/2305711/\">Формула Миффлина-Сан Жеора</a>")
    lines.append("• <a href=\"https://journals.physiology.org/doi/full/10.1152/ajpendo.00156.2017\">Метаболические расчеты</a>")
    lines.append("• <a href=\"https://ceur-ws.org/Vol-3806/S_42_Pleskach.pdf\">Системы подсчета калорий</a>")

    lines.append("")
    lines.append(_("Оставим так или что-то скорректируем?"))

    kb = _ikb([
        [("Все отлично", "final:ok")],
        [("Хочу скорректировать", "final:adjust")],
    ])

    await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
    await state.set_state(OnboardingStates.review)


# =====================
# Стартовый экран
# =====================

@router.message(Command("onboarding"))
@router.message(Command("onbording"))  # alias for common typo
async def cmd_onboarding(message: Message, state: FSMContext) -> None:
    logger.info("/onboarding command received | from_user={} | chat_id={}", getattr(message.from_user, 'id', None), getattr(message.chat, 'id', None))
    await state.clear()
    text = _(
        "Привет! 👋\n"
        "Я помогаю поддерживать фигуру с помощью контроля калорий и БЖУ.\n\n"
        "Процесс максимально простой:\n"
        "1. Определяем начальные показатели и цели\n"
        "2. Рассчитываем необходимое потребление калорий с балансом БЖУ\n"
        "3. Каждый день на основе фото или описания блюд считаем калории и, при необходимости, корректируем рацион\n\n"
        "Это гораздо удобнее, чем считать калории вручную, поэтому по статистике наши пользователи в 2 раза чаще достигают поставленных целей.\n\n"
        "Приступим? 🚀"
    )
    kb = _ikb([[("Начнем", "onboarding_start")]])
    await message.answer(text, reply_markup=kb)


# Allow launching from inline menu button (backward compat)
@router.callback_query(F.data == "onboarding")
async def cb_onboarding(call: CallbackQuery, state: FSMContext) -> None:
    await cmd_onboarding(call.message, state)  # type: ignore[arg-type]
    await call.answer()


@router.callback_query(F.data == "onboarding_start")
async def cb_onboarding_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.gender)
    caption = _("Теперь нужно собрать начальные показатели, чтобы составить план. Начнём с выбора пола")
    kb = _ikb([
        [("Я мужчина", "gender:male")],
        [("Я девушка", "gender:female")],
    ])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await call.message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await call.message.answer(caption, reply_markup=kb)
    await call.answer()


# =====================
# Пол
# =====================

@router.callback_query(OnboardingStates.gender, F.data.startswith("gender:"))
async def cb_gender(call: CallbackQuery, state: FSMContext) -> None:
    gender = call.data.split(":", 1)[1]
    await state.update_data(gender=gender)
    await state.set_state(OnboardingStates.age)
    await call.message.answer(_("Сколько тебе лет?"))
    await call.answer()


# Текстовый fallback (male/female)
@router.message(OnboardingStates.gender, F.text.casefold().in_(["male", "female"]))
async def gender_set(message: Message, state: FSMContext) -> None:
    await state.update_data(gender=message.text.strip().lower())
    await state.set_state(OnboardingStates.age)
    await message.answer(_("Сколько тебе лет?"))


@router.message(OnboardingStates.gender)
async def gender_retry(message: Message) -> None:
    await message.answer(_("Введи male или female"))


# =====================
# Возраст / Вес / Рост / Активность
# =====================

@router.message(OnboardingStates.age, F.text.regexp(r"^\d{1,3}$"))
async def age_set(message: Message, state: FSMContext) -> None:
    age = int(message.text)
    if not (1 <= age <= 120):
        await message.answer(_("Пожалуйста, введите корректный возраст (от 1 до 120 лет)"))
        return
    await state.update_data(age=age)
    await state.set_state(OnboardingStates.weight)
    await message.answer(_("Какой у тебя текущий вес в килограммах?"))


@router.message(OnboardingStates.age)
async def age_retry(message: Message) -> None:
    await message.answer(_("Пожалуйста, введите корректный возраст (от 1 до 120 лет)"))


@router.message(OnboardingStates.weight, F.text.regexp(r"^\d{2,3}([.,]\d{1,2})?$"))
async def weight_set(message: Message, state: FSMContext) -> None:
    w = float(message.text.replace(",", "."))
    if not (30 <= w <= 300):
        await message.answer(_("Пожалуйста, введите корректный вес (от 30 до 300 килограммов)"))
        return
    await state.update_data(weight_kg=w)
    await state.set_state(OnboardingStates.height)
    await message.answer(_("Какой у тебя рост в сантиметрах?"))


@router.message(OnboardingStates.weight)
async def weight_retry(message: Message) -> None:
    await message.answer(_("Пожалуйста, введите корректный вес (от 30 до 300 килограммов)"))


@router.message(OnboardingStates.height, F.text.regexp(r"^\d{3}$"))
async def height_set(message: Message, state: FSMContext) -> None:
    h = float(message.text)
    if not (120 <= h <= 250):
        await message.answer(_("Пожалуйста, введите корректный рост (от 120 до 250 см)"))
        return
    await state.update_data(height_cm=h)
    await state.set_state(OnboardingStates.activity)
    await message.answer(
        _(
            "Опиши, пожалуйста, свою повседневную активность. Так мы сможем учесть уровень активности в плане питания, чтобы он был максимально точным.\n\n"
            "Например:\nВ среднем хожу 7-10 тысяч шагов в день, 2 раза в неделю тренируюсь в зале, 1 раз в неделю бегаю."
        )
    )


@router.message(OnboardingStates.height)
async def height_retry(message: Message) -> None:
    await message.answer(_("Пожалуйста, введите корректный рост (от 120 до 250 см)"))


@router.message(OnboardingStates.activity, F.text.len() >= 10)
async def activity_set(message: Message, state: FSMContext) -> None:
    await state.update_data(activity_text=message.text.strip())
    await message.answer(_("Анализирую уровень активности... ⏳"))
    await state.set_state(OnboardingStates.goal)

    # Показ целей с inline-кнопками
    text = _(
        "Зафиксировал! Теперь самое главное — поставим цель\n"
        "TapTap  помогает достигать долгосрочных результатов благодаря развитию полезных привычек"
    )
    kb = _ikb([
        [("Хочу похудеть", "goal:lose")],
        [("Хочу набрать мышечную массу", "goal:gain")],
        [("Хочу поддерживать текущий вес", "goal:maintain")],
    ])
    await message.answer(text, reply_markup=kb)


@router.message(OnboardingStates.activity)
async def activity_retry(message: Message) -> None:
    await message.answer(_("Пожалуйста, опишите вашу активность подробнее (минимум 10 символов)"))


# =====================
# Цель
# =====================

@router.callback_query(OnboardingStates.goal, F.data.startswith("goal:"))
async def cb_goal(call: CallbackQuery, state: FSMContext) -> None:
    goal_raw = call.data.split(":", 1)[1]
    await state.update_data(goal=goal_raw)

    if goal_raw == Goal.maintain.value:
        # Миновать скорость и целевой вес — сразу финализация
        await state.set_state(OnboardingStates.speed)
        await _finalize_and_show(call.message, state, call.from_user.id)  
        await call.answer()
        return

    # Для lose/gain спросим целевой вес
    await state.set_state(OnboardingStates.goal_weight)
    await call.message.answer(_("К какому весу ты стремишься?"))
    await call.answer()


# Текстовый fallback цели
@router.message(OnboardingStates.goal, F.text.casefold().in_(["lose", "gain", "maintain"]))
async def goal_set(message: Message, state: FSMContext) -> None:
    goal_raw = message.text.strip().lower()
    await state.update_data(goal=goal_raw)

    if goal_raw == "maintain":
        await state.set_state(OnboardingStates.speed)
        await _finalize_and_show(message, state, message.from_user.id)
        return

    await state.set_state(OnboardingStates.goal_weight)
    await message.answer(_("К какому весу стремишься? Укажи в кг (например: 75.0)"))


@router.message(OnboardingStates.goal)
async def goal_retry(message: Message) -> None:
    await message.answer(_("Введи одну из целей: lose | gain | maintain"))


# =====================
# Целевой вес -> кнопки скорости
# =====================

@router.message(OnboardingStates.goal_weight, F.text.regexp(r"^\d{2,3}([.,]\d{1,2})?$"))
async def goal_weight_set(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_w = float(data.get("weight_kg"))
    goal_w = float(message.text.replace(",", "."))
    goal_raw = str(data.get("goal"))

    # Бизнес-валидация
    if goal_raw == "lose" and goal_w >= current_w:
        await message.answer(_("Для похудения целевой вес должен быть меньше текущего. Попробуй ещё раз."))
        return
    if goal_raw == "gain" and goal_w <= current_w:
        await message.answer(_("Для набора массы целевой вес должен быть больше текущего. Попробуй ещё раз."))
        return

    await state.update_data(goal_weight_kg=goal_w)
    await state.set_state(OnboardingStates.speed)

    # Динамические N кг/нед
    comfort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.comfort])
    effort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.effort])
    fast = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.fast])

    kb = _ikb([
        [(f"С комфортом {comfort} кг в неделю", "speed:COMFORT")],
        [(f"С усилием {effort} кг в неделю", "speed:EFFORT")],
        [(f"Ускоренно {fast} кг в неделю", "speed:FAST")],
    ])
    await message.answer(_("Как быстро хочешь достичь цели?"), reply_markup=kb)


@router.message(OnboardingStates.goal_weight)
async def goal_weight_retry(message: Message) -> None:
    await message.answer(_("Некорректный формат. Пример: 75.0"))


# =====================
# Выбор скорости (кнопки) -> финализация
# =====================

@router.callback_query(OnboardingStates.speed, F.data.startswith("speed:"))
async def cb_speed(call: CallbackQuery, state: FSMContext) -> None:
    speed_raw = call.data.split(":", 1)[1]
    await state.update_data(speed=speed_raw)
    await _finalize_and_show(call.message, state, call.from_user.id)
    await call.answer()


# Fallback: ввод скорости текстом
@router.message(OnboardingStates.speed)
async def speed_and_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()

    speed_raw = message.text.strip().upper() if message.text else None
    speed = None
    if speed_raw and speed_raw != "-":
        if speed_raw not in {"COMFORT", "EFFORT", "FAST"}:
            await message.answer(_("Скорость не распознана. Используй: COMFORT | EFFORT | FAST | '-' для пропуска"))
            return
        speed = Speed(speed_raw)
        await state.update_data(speed=speed.value)

    await _finalize_and_show(message, state, message.from_user.id)


# =====================
# Финальный экран: OK / Adjust
# =====================

@router.callback_query(OnboardingStates.review, F.data == "final:ok")
async def cb_final_ok(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await call.message.answer(_("Отлично! Продолжим позже с оплатой."))


@router.callback_query(OnboardingStates.review, F.data == "final:adjust")
async def cb_final_adjust(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.adjust)
    kb = _ikb([[("Вернуться", "final:back")]])
    await call.message.answer(_("Напиши, в свободном формате, что нужно скорректировать в твоём индивидуальном плане"), reply_markup=kb)
    await call.answer()


@router.callback_query(OnboardingStates.adjust, F.data == "final:back")
async def cb_final_back(call: CallbackQuery, state: FSMContext) -> None:
    # Показать финальный экран снова
    await _finalize_and_show(call.message, state, call.from_user.id)
    await call.answer()


@router.message(OnboardingStates.adjust)
async def adjust_apply(message: Message, state: FSMContext) -> None:
    # Имитация корректировок: сообщим и покажем план повторно
    await message.answer(_("Твой план скорректирован!"))
    await _finalize_and_show(message, state, message.from_user.id)
