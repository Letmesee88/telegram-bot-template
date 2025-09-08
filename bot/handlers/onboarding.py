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
from sqlalchemy import select, update, func
from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.services.analytics import analytics

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, UserModel
from bot.schemas.onboarding import ActivityLevel, Gender, Goal, OnboardingData, Speed
from bot.services.plan import (
    calculate_daily_plan,
    _infer_activity_level as infer_activity_level,
    SPEED_PERCENT_BY_WEIGHT,
)
from bot.handlers import start as start_module

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
        await message.answer(_("Данные не прошли валидацию. Попробуй заново: /start"))
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
        await message.answer(_("Не удалось сохранить данные. Попробуй ещё раз или позже: /start"))
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
        [("Отлично", "final:ok")],
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
    logger.info("/onboarding command received -> redirect to /start | from_user={} | chat_id={}", getattr(message.from_user, 'id', None), getattr(message.chat, 'id', None))
    # Soft-redirect: показываем единый стартовый экран с корректным ветвлением
    await start_module.start_handler(message, state)


# Allow launching from inline menu button (backward compat)
@router.callback_query(F.data == "onboarding")
async def cb_onboarding(call: CallbackQuery, state: FSMContext) -> None:
    logger.info("cb_onboarding | user_id={} | chat_id={}", getattr(call.from_user, 'id', None), getattr(call.message.chat, 'id', None))
    await cmd_onboarding(call.message, state)  # type: ignore[arg-type]
    await call.answer()


@router.callback_query(F.data == "onboarding_start")
async def cb_onboarding_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.gender)
    caption = _("Теперь нужно собрать начальные показатели, чтобы составить план. Начнём с выбора пола")
    kb = _ikb([
        [("Я мужчина", "gender:male"), ("Я девушка", "gender:female")],
    ])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await call.message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await call.message.answer(caption, reply_markup=kb)
    await call.answer()


# =====================
# Возобновление/перезапуск онбординга
# =====================

async def _ask_gender(message: Message) -> None:
    caption = _("Теперь нужно собрать начальные показатели, чтобы составить план. Начнём с выбора пола")
    kb = _ikb([[ ("Я мужчина", "gender:male"), ("Я девушка", "gender:female") ]])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await message.answer(caption, reply_markup=kb)


async def _ask_age(message: Message) -> None:
    await message.answer(_("Сколько тебе лет?"))


async def _ask_weight(message: Message) -> None:
    await message.answer(_("Какой у тебя текущий вес в килограммах?"))


async def _ask_height(message: Message) -> None:
    await message.answer(_("Какой у тебя рост в сантиметрах?"))


async def _ask_activity(message: Message) -> None:
    await message.answer(
        _(
            "Опиши, пожалуйста, свою повседневную активность. Так мы сможем учесть уровень активности в плане питания, чтобы он был максимально точным.\n\n"
            "Например:\nВ среднем хожу 7-10 тысяч шагов в день, 2 раза в неделю тренируюсь в зале, 1 раз в неделю бегаю."
        )
    )


async def _ask_goal(message: Message) -> None:
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


async def _ask_goal_weight(message: Message) -> None:
    await message.answer(_("К какому весу ты стремишься?"))


async def _ask_speed(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_w = float(data.get("weight_kg")) if data.get("weight_kg") is not None else None
    if current_w is None:
        kb_simple = _ikb([
            [("С комфортом", "speed:COMFORT")],
            [("С усилием", "speed:EFFORT")],
            [("Ускоренно", "speed:FAST")],
        ])
        await message.answer(_("Как быстро хочешь достичь цели?"), reply_markup=kb_simple)
        return
    comfort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.comfort])
    effort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.effort])
    fast = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.fast])
    kb = _ikb([
        [(f"С комфортом {comfort} кг в неделю", "speed:COMFORT")],
        [(f"С усилием {effort} кг в неделю", "speed:EFFORT")],
        [(f"Ускоренно {fast} кг в неделю", "speed:FAST")],
    ])
    await message.answer(_("Как быстро хочешь достичь цели?"), reply_markup=kb)


@router.callback_query(F.data == "onboarding_resume")
async def cb_onboarding_resume(call: CallbackQuery, state: FSMContext) -> None:
    # Analytics
    if analytics.logger and call.from_user:
        analytics.fire_event(
            BaseEvent(
                user_id=call.from_user.id,
                event_type="Onboarding:Resume",
                event_properties=EventProperties(
                    chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                    chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                    text=None,
                    command="/start",
                ),
                language=getattr(call.from_user, 'language_code', None),
                plan=Plan(branch="InProgress", source="start", version="v1"),
            )
        )

    cur = await state.get_state()
    if cur is None:
        await cb_onboarding_start(call, state)
        return

    if cur == OnboardingStates.gender.state:
        await _ask_gender(call.message)
    elif cur == OnboardingStates.age.state:
        await _ask_age(call.message)
    elif cur == OnboardingStates.weight.state:
        await _ask_weight(call.message)
    elif cur == OnboardingStates.height.state:
        await _ask_height(call.message)
    elif cur == OnboardingStates.activity.state:
        await _ask_activity(call.message)
    elif cur == OnboardingStates.goal.state:
        await _ask_goal(call.message)
    elif cur == OnboardingStates.goal_weight.state:
        await _ask_goal_weight(call.message)
    elif cur == OnboardingStates.speed.state:
        await _ask_speed(call.message, state)
    elif cur == OnboardingStates.review.state:
        await _finalize_and_show(call.message, state, call.from_user.id)
    elif cur == OnboardingStates.adjust.state:
        kb = _ikb([[ ("Вернуться", "final:back") ]])
        await call.message.answer(_("Напиши, в свободном формате, что нужно скорректировать в твоём индивидуальном плане"), reply_markup=kb)
    else:
        # Fallback — начнем сначала
        await cb_onboarding_start(call, state)
        return

    await call.answer()


@router.callback_query(F.data == "onboarding_restart")
async def cb_onboarding_restart(call: CallbackQuery, state: FSMContext) -> None:
    # Analytics
    if analytics.logger and call.from_user:
        analytics.fire_event(
            BaseEvent(
                user_id=call.from_user.id,
                event_type="Onboarding:Restart",
                event_properties=EventProperties(
                    chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                    chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                    text=None,
                    command="/start",
                ),
                language=getattr(call.from_user, 'language_code', None),
                plan=Plan(branch="Restart", source="start", version="v1"),
            )
        )

    await state.clear()
    await cb_onboarding_start(call, state)


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


# Текстовый fallback (male/female) — запрещаем свободный ввод, повторяем шаг с кнопками
@router.message(OnboardingStates.gender, F.text.casefold().in_(["male", "female"]))
async def gender_set(message: Message, state: FSMContext) -> None:
    caption = _("Теперь нужно собрать начальные показатели, чтобы составить план. Начнём с выбора пола")
    kb = _ikb([
        [("Я мужчина", "gender:male"), ("Я девушка", "gender:female")],
    ])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await message.answer(caption, reply_markup=kb)


# На шаге выбора пола любые сообщения — только кнопки
@router.message(OnboardingStates.gender)
async def gender_retry(message: Message) -> None:
    caption = _("Теперь нужно собрать начальные показатели, чтобы составить план. Начнём с выбора пола")
    kb = _ikb([
        [("Я мужчина", "gender:male"), ("Я девушка", "gender:female")],
    ])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await message.answer(caption, reply_markup=kb)


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


# Текстовый fallback цели — запрещаем свободный ввод, повторяем шаг с кнопками
@router.message(OnboardingStates.goal, F.text.casefold().in_(["lose", "gain", "maintain"]))
async def goal_set(message: Message, state: FSMContext) -> None:
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


@router.message(OnboardingStates.goal)
async def goal_retry(message: Message) -> None:
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


# Fallback: ввод скорости текстом — запрещаем свободный ввод, повторяем шаг с кнопками
@router.message(OnboardingStates.speed)
async def speed_and_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_w = float(data.get("weight_kg")) if data.get("weight_kg") is not None else None

    # Если нет веса в состоянии, просто просим выбрать кнопку ещё раз
    if current_w is None:
        kb = _ikb([
            [("С комфортом", "speed:COMFORT")],
            [("С усилием", "speed:EFFORT")],
            [("Ускоренно", "speed:FAST")],
        ])
        await message.answer(_("Как быстро хочешь достичь цели?"), reply_markup=kb)
        return

    comfort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.comfort])
    effort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.effort])
    fast = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.fast])

    kb = _ikb([
        [(f"С комфортом {comfort} кг в неделю", "speed:COMFORT")],
        [(f"С усилием {effort} кг в неделю", "speed:EFFORT")],
        [(f"Ускоренно {fast} кг в неделю", "speed:FAST")],
    ])
    await message.answer(_("Как быстро хочешь достичь цели?"), reply_markup=kb)


# =====================
# Финальный экран: OK / Adjust
# =====================

@router.callback_query(OnboardingStates.review, F.data == "final:ok")
async def cb_final_ok(call: CallbackQuery, state: FSMContext) -> None:
    # Активируем FoodAI для пользователя (временный гейтинг до оплаты)
    try:
        async with sessionmaker() as session:
            await session.execute(
                update(UserModel)
                .where(UserModel.id == call.from_user.id)
                .values(foodai_enabled_at=func.now())
            )
            await session.commit()
    except Exception as e:
        logger.exception("onboarding.final.ok.update_user_failed | user_id={} | error={}", getattr(call.from_user, 'id', None), e)

    await call.answer()
    await state.clear()
    await call.message.answer(
        _(
            "Готово! Я активировал распознавание еды (FoodAI). Отправь фото блюда — я проанализирую калории и БЖУ."
        )
    )


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
