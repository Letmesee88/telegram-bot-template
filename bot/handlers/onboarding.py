from __future__ import annotations

import asyncio
from time import perf_counter
from aiogram import F, Router
from aiogram.enums import ChatAction

import re
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    BufferedInputFile,
)
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select, update, func
from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.services.analytics import analytics

from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, UserModel
from bot.schemas.onboarding import ActivityLevel, Gender, Goal, OnboardingData, Speed, DailyPlan
from bot.services.plan import (
    calculate_daily_plan,
    _infer_activity_level as infer_activity_level,
    SPEED_PERCENT_BY_WEIGHT,
)
from bot.services.llm_activity import classify_activity_cached
from bot.services.adjust import (
    parse_adjustment_cached,
    apply_adjustment,
    parse_adjustment_heuristic,
    rephrase_explanation_cached,
)
from bot.core.config import settings
from bot.handlers import start as start_module
from bot.services.charts import get_plan_chart_png
from datetime import date
import hashlib

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

    # Определим уровень активности. Если ранее зафиксировали в FSM — используем его и не вызываем LLM повторно
    level: ActivityLevel
    llm_obj = None
    llm_used = False
    pre_level_raw = data.get("activity_level")
    if isinstance(pre_level_raw, str) and pre_level_raw in {"sedentary","light","moderate","active","athlete"}:
        level = ActivityLevel(pre_level_raw)
    else:
        try:
            llm_obj = await classify_activity_cached(user_id, payload.activity_text, lang_hint=getattr(message.from_user, 'language_code', None))
            if llm_obj and isinstance(getattr(llm_obj, 'level', None), str):
                lvl = (llm_obj.level or '').strip().lower()
                conf = float(getattr(llm_obj, 'confidence', 0.0) or 0.0)
                if lvl in {"sedentary", "light", "moderate", "active", "athlete"} and conf >= 0.6:
                    level = ActivityLevel(lvl)
                    if level == ActivityLevel.athlete:
                        features = (getattr(llm_obj, 'features', {}) or {})
                        wpw_raw = features.get('workouts_per_week')
                        wpw_num = None
                        try:
                            if isinstance(wpw_raw, (int, float)):
                                wpw_num = int(wpw_raw)
                            elif isinstance(wpw_raw, str):
                                s = wpw_raw.strip()
                                # extract first integer (supports "5–6", "5-6", "6+", "6 раз")
                                m = re.search(r"(\d+)", s)
                                if m:
                                    wpw_num = int(m.group(1))
                        except Exception:
                            wpw_num = None
                        if wpw_num is not None and wpw_num < 6:
                            level = ActivityLevel.active
                    llm_used = True
                else:
                    level = infer_activity_level(payload.activity_text)
            else:
                level = infer_activity_level(payload.activity_text)
        except Exception as e:
            logger.warning("onboarding.activity.llm_failed | user_id={} | err={}", user_id, e)
            level = infer_activity_level(payload.activity_text)

    # гарантируем, что расчёт плана использует определённый уровень
    try:
        payload = payload.model_copy(update={"activity_level": level})
    except Exception:
        try:
            payload.activity_level = level  # type: ignore[attr-defined]
        except Exception:
            pass

    plan = calculate_daily_plan(payload)

    # Сохранение в БД (upsert)
    try:
        async with sessionmaker() as session:
            existing = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == payload.user_id)
            )

            data_json = payload.model_dump(mode="json")
            data_json["activity_level"] = level.value
            if llm_used and llm_obj is not None:
                try:
                    data_json["activity_llm"] = {
                        "level": getattr(llm_obj, 'level', None),
                        "confidence": getattr(llm_obj, 'confidence', None),
                        "features": getattr(llm_obj, 'features', {}) or {},
                        "rationale": getattr(llm_obj, 'rationale', None),
                        "version": getattr(llm_obj, 'version', 'v1'),
                    }
                except Exception:
                    pass

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
            logger.info("adjust.saved | user_id={} | adjustments_count={}", user_id, len(data_json.get("adjustments") or []))
    except Exception as e:
        logger.exception("onboarding.finalize.db_error | user_id={} | error={}", payload.user_id, e)
        await message.answer(_("Не удалось сохранить данные. Попробуй ещё раз или позже: /start"))
        return

    # Сформировать финальный текст согласно ТЗ
    lines: list[str] = []
    lines.append("<b>" + _("Твой индивидуальный план готов!") + "</b>")
    lines.append("")

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

    # Попробуем отправить график с подписью (в идеале — весь текст как caption)
    try:
        if settings.CHARTS_ENABLED:
            start_w = float(payload.weight_kg)
            goal_w = float(payload.goal_weight_kg) if payload.goal_weight_kg is not None else None
            weekly = float(getattr(plan, 'weekly_rate_kg', 0.0) or 0.0)
            start_dt = getattr(message, 'date', None)
            start_d = start_dt.date() if start_dt else date.today()
            eta = getattr(plan, 'eta_date', None)
            logger.info("charts.try_send | phase=finalize | user_id={} | weekly={} | eta={}", payload.user_id, weekly, eta)
            key_str = f"{start_w}:{goal_w}:{weekly}:{start_d.isoformat()}:{eta.isoformat() if eta else ''}:{settings.CHARTS_PRIVACY_MODE}:{settings.CHARTS_BAND_FRAC}"
            ph = hashlib.sha256(key_str.encode('utf-8')).hexdigest()[:16]
            png = await get_plan_chart_png(payload.user_id, ph,
                                           start_weight=start_w,
                                           goal_weight=goal_w,
                                           weekly_rate=weekly,
                                           start_date=start_d,
                                           eta_date=eta)
            if png:
                caption = "\n".join(lines)
                # Telegram ограничивает caption у фото (~1024 символа). Если не помещается — отправим короткую подпись.
                if len(caption) <= 1024:
                    await message.answer_photo(BufferedInputFile(png, filename="goal_plan.png"), caption=caption, reply_markup=kb)
                    await state.set_state(OnboardingStates.review)
                    return
                else:
                    await message.answer_photo(BufferedInputFile(png, filename="goal_plan.png"), caption=lines[0])
                    await message.answer(caption, reply_markup=kb, disable_web_page_preview=True)
                    await state.set_state(OnboardingStates.review)
                    return
    except Exception as e:
        logger.warning("charts.send_failed_caption | user_id={} | err={}", payload.user_id, e)

    # Фолбэк: если график отключен или не загрузился — шлём текстом
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
    try:
        photo = FSInputFile("bot/static/charts.jpg")
        await message.answer_photo(photo, caption=text, reply_markup=kb)
    except Exception:
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
@router.message(OnboardingStates.gender, F.text & (~F.text.startswith("/")))
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


@router.message(OnboardingStates.age, F.text & (~F.text.startswith("/")))
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


@router.message(OnboardingStates.weight, F.text & (~F.text.startswith("/")))
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


@router.message(OnboardingStates.height, F.text & (~F.text.startswith("/")))
async def height_retry(message: Message) -> None:
    await message.answer(_("Пожалуйста, введите корректный рост (от 120 до 250 см)"))


@router.message(OnboardingStates.activity, F.text.len() >= 10)
async def activity_set(message: Message, state: FSMContext) -> None:
    # Сохраняем текст и сразу классифицируем активность (LLM с таймаутом и фолбэком)
    text_raw = (message.text or "").strip()
    await state.update_data(activity_text=text_raw)

    # Сообщение пользователю и гарантия минимальной задержки 0.3с
    await message.answer(_("✨ Анализирую уровень активности..."))
    t0 = perf_counter()

    # Попытка LLM → даунгрейд правил athlete → фолбэк эвристика
    level: ActivityLevel
    try:
        llm_obj = await classify_activity_cached(
            getattr(message.from_user, 'id', 0),
            text_raw,
            lang_hint=getattr(message.from_user, 'language_code', None),
        )
        if llm_obj and isinstance(getattr(llm_obj, 'level', None), str):
            lvl = (llm_obj.level or '').strip().lower()
            conf = float(getattr(llm_obj, 'confidence', 0.0) or 0.0)
            if lvl in {"sedentary", "light", "moderate", "active", "athlete"} and conf >= 0.6:
                level = ActivityLevel(lvl)
                if level == ActivityLevel.athlete:
                    features = (getattr(llm_obj, 'features', {}) or {})
                    wpw_raw = features.get('workouts_per_week')
                    wpw_num = None
                    try:
                        if isinstance(wpw_raw, (int, float)):
                            wpw_num = int(wpw_raw)
                        elif isinstance(wpw_raw, str):
                            s = wpw_raw.strip()
                            m = re.search(r"(\d+)", s)
                            if m:
                                wpw_num = int(m.group(1))
                    except Exception:
                        wpw_num = None
                    if wpw_num is not None and wpw_num < 6:
                        level = ActivityLevel.active
            else:
                level = infer_activity_level(text_raw)
        else:
            level = infer_activity_level(text_raw)
    except Exception as e:
        logger.warning("activity.inline.llm_failed | user_id={} | err={}", getattr(message.from_user, 'id', None), e)
        level = infer_activity_level(text_raw)

    # Сохраняем определённый уровень в FSM
    await state.update_data(activity_level=level.value)

    # Гарантируем минимальную задержку 0.3с (если LLM ответил быстрее)
    dt = perf_counter() - t0
    if dt < 0.3:
        await asyncio.sleep(0.3 - dt)

    # Переходим к выбору цели
    await state.set_state(OnboardingStates.goal)
    # Показ целей: одно сообщение с фото+caption+инлайн-кнопками
    await _ask_goal(message)


@router.message(OnboardingStates.activity, F.text & (~F.text.startswith("/")))
async def activity_retry(message: Message) -> None:
    await message.answer(_("Пожалуйста, опишите вашу активность подробнее (минимум 10 символов)"))


# =====================
# Цель
# =====================

@router.callback_query(OnboardingStates.goal, F.data.startswith("goal:"))
async def cb_goal(call: CallbackQuery, state: FSMContext) -> None:
    # Гасим спиннер сразу (даже при повторном клике)
    try:
        await call.answer()
    except Exception:
        pass

    # Идемпотентный guard: состояние и лок
    cur_state = await state.get_state()
    if cur_state != OnboardingStates.goal.state:
        try:
            await call.answer(_("Уже обработано"), cache_time=3)
        except Exception:
            pass
        return
    data = await state.get_data()
    if data.get("goal_locked") is True:
        try:
            await call.answer(_("Уже обработано"), cache_time=3)
        except Exception:
            pass
        return
    await state.update_data(goal_locked=True)

    goal_raw = call.data.split(":", 1)[1]
    await state.update_data(goal=goal_raw)

    # Analytics: выбор цели
    if analytics.logger and call.from_user:
        try:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Onboarding:GoalSelected",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                        chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, 'language_code', None),
                    plan=Plan(branch="SetGoal", source="onboarding", version="v1"),
                )
            )
        except Exception:
            pass

    if goal_raw == Goal.maintain.value:
        # Для maintain: оставляем сообщение как есть, сразу финализация
        await state.set_state(OnboardingStates.speed)
        await _finalize_and_show(call.message, state, call.from_user.id)
        return

    # Для lose/gain: удалим сообщение с картинкой и кнопками (чтобы оно исчезло из чата)
    try:
        await call.message.delete()
    except Exception:
        # Фолбэк: если удалить не удалось (например, ограничения клиента), уберём хотя бы клавиатуру
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    # Сразу задаём следующий вопрос отдельным фото-сообщением
    await state.set_state(OnboardingStates.goal_weight)
    try:
        photo = FSInputFile("bot/static/charts.jpg")
        await call.message.answer_photo(photo, caption=_("К какому весу ты стремишься?"))
    except Exception:
        await call.message.answer(_("К какому весу ты стремишься?"))



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


@router.message(OnboardingStates.goal, F.text & (~F.text.startswith("/")))
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


@router.message(OnboardingStates.goal_weight, F.text & (~F.text.startswith("/")))
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
@router.message(OnboardingStates.speed, F.text & (~F.text.startswith("/")))
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
    user_id = message.from_user.id
    text = (message.text or "").strip()
    # Immediate UX feedback while we process
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    except Exception:
        pass
    await message.answer(_("✨ Изучаю ваши пожелания и обновляю план..."))
    logger.info(
        "adjust.enter | user_id={} | text_len={} | state=OnboardingStates.adjust",
        user_id,
        len(text),
    )

    # 1) Получаем последнюю запись онбординга
    try:
        async with sessionmaker() as session:
            existing = await session.scalar(
                select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
            )
            if not existing:
                await message.answer(_("Не нашёл данные онбординга. Попробуй начать заново: /start"))
                return

            data_json = dict(existing.data or {})
            dp_json = dict(existing.daily_plan or {})
            logger.info(
                "adjust.payload_loaded | user_id={} | has_base_plan={} | has_daily_plan={}",
                user_id,
                bool(data_json.get("base_plan")),
                bool(dp_json),
            )

            # 2) Восстановим объекты для вычислений
            try:
                base_plan_dict = data_json.get("base_plan") or dp_json
                base_plan = DailyPlan.model_validate(base_plan_dict)
            except Exception:
                # Фолбэк: соберём base_plan из current
                base_plan = DailyPlan.model_validate(dp_json)
                data_json["base_plan"] = base_plan.model_dump(mode="json")

            try:
                payload = OnboardingData.model_validate(data_json)
            except Exception as e:
                logger.warning("adjust.payload_invalid | user_id={} | err={}", user_id, e)
                await message.answer(_("Данные онбординга повреждены. Попробуй заново: /start"))
                return

            # 3) Парсинг корректировки через LLM (с кешем)
            parsed = await parse_adjustment_cached(
                user_id, text, lang_hint=getattr(message.from_user, 'language_code', None)
            )
            if not parsed:
                # Heuristic fallback for top intents (offline, RU)
                h = parse_adjustment_heuristic(text)
                if h:
                    logger.info("adjust.heuristic_used | user_id={} | intents={} | text_len={}", user_id, h.intents, len(text))
                    parsed = h
                else:
                    await message.answer(
                        _("Не до конца понял запрос. Сформулируй одной фразой, например: \n• 'уберите углеводы' \n• 'добавь 200 ккал' \n• 'мало двигаюсь — поставь низкую активность'"))
                    return
            else:
                logger.info(
                    "adjust.parsed | user_id={} | intents={} | activity_override={} | calories={} | macros={} | conf={}",
                    user_id,
                    getattr(parsed, 'intents', None),
                    getattr(parsed, 'activity_override', None),
                    getattr(parsed, 'calories', None),
                    getattr(parsed, 'macros', None),
                    getattr(parsed, 'confidence', None),
                )

            # 4) Применим детерминированные правила
            new_plan, explanation, summary = apply_adjustment(base_plan, payload, parsed)
            logger.info(
                "adjust.applied | user_id={} | calories={} | p/f/c={}/{}/{}",
                user_id,
                new_plan.calories,
                new_plan.protein_g,
                new_plan.fat_g,
                new_plan.carbs_g,
            )

            # Сформируем персональную заметку (без чисел), чтобы текст был менее шаблонным
            personal_line: str | None = None
            try:
                note = getattr(parsed, 'rationale', None)
                intents = list(getattr(parsed, 'intents', []) or [])
                if isinstance(note, str) and note.strip():
                    personal_line = _("Учёл запрос: ") + note.strip()
                else:
                    intent_map = {
                        'low_fodmap_candidate': _("уменьшить FODMAP-продукты"),
                        'lactose_free': _("избегать лактозы"),
                        'gluten_free': _("без глютена"),
                        'sugar_free': _("ограничить сахар"),
                        'keto': _("кето-схему"),
                        'low_carb': _("снизить углеводы"),
                        'high_protein': _("акцент на белок"),
                        'raise_calories': _("увеличить калорийность"),
                        'lower_calories': _("снизить калорийность"),
                        'activity_down': _("понизить активность"),
                        'activity_up': _("повысить активность"),
                        'reduce_protein': _("снизить белок"),
                        'reduce_fat': _("снизить жиры"),
                        'increase_fat': _("повысить жиры"),
                        'custom_macros': _("кастомные макросы"),
                    }
                    phrases = [intent_map[i] for i in intents if i in intent_map]
                    if phrases:
                        personal_line = _("Учёл запрос: ") + ", ".join(phrases)
            except Exception:
                personal_line = None

            # 4.1) Гибрид (асинхронно): перефразировать объяснение в фоне и, если успеет, отредактировать сообщение
            should_try_rephrase = (
                settings.ADJUST_REPHRASE_ENABLED
                and explanation
                and len(explanation) >= int(getattr(settings, "ADJUST_REPHRASE_LENGTH_MIN", 220) or 220)
            )

            # 5) Сохраним
            adjustments = list(data_json.get("adjustments") or [])
            adjustments.append({
                "ts": getattr(message, 'date', None).isoformat() if getattr(message, 'date', None) else None,
                "text_raw": text,
                "parsed": {
                    "intents": getattr(parsed, 'intents', None),
                    "activity_override": getattr(parsed, 'activity_override', None),
                    "calories": getattr(parsed, 'calories', None),
                    "macros": getattr(parsed, 'macros', None),
                    "dietary_restrictions": getattr(parsed, 'dietary_restrictions', None),
                    "confidence": getattr(parsed, 'confidence', None),
                    "version": getattr(parsed, 'version', None),
                },
                "applied": summary,
            })
            data_json["adjustments"] = adjustments

            existing.data = data_json
            existing.daily_plan = new_plan.model_dump(mode="json")
            existing.calories = new_plan.calories

            await session.commit()

    except Exception as e:
        logger.exception("adjust.apply_failed | user_id={} | err={}", user_id, e)
        await message.answer(_("Не удалось применить корректировку. Попробуй ещё раз позже."))
        return

    # Попробуем подготовить обновленный график (без немедленной отправки — вложим как caption ниже)
    png_data = None
    try:
        if settings.CHARTS_ENABLED:
            start_w = float(payload.weight_kg)
            goal_w = float(payload.goal_weight_kg) if payload.goal_weight_kg is not None else None
            weekly = float(getattr(new_plan, 'weekly_rate_kg', 0.0) or 0.0)
            start_dt = getattr(message, 'date', None)
            start_d = start_dt.date() if start_dt else date.today()
            eta = getattr(new_plan, 'eta_date', None)
            logger.info("charts.try_send | phase=adjust | user_id={} | weekly={} | eta={}", user_id, weekly, eta)
            key_str = f"{start_w}:{goal_w}:{weekly}:{start_d.isoformat()}:{eta.isoformat() if eta else ''}:{settings.CHARTS_PRIVACY_MODE}:{settings.CHARTS_BAND_FRAC}:adjust"
            ph = hashlib.sha256(key_str.encode('utf-8')).hexdigest()[:16]
            png = await get_plan_chart_png(user_id, ph,
                                           start_weight=start_w,
                                           goal_weight=goal_w,
                                           weekly_rate=weekly,
                                           start_date=start_d,
                                           eta_date=eta)
            if png:
                logger.info("charts.photo_ready | phase=adjust | bytes={}", len(png))
                png_data = png
    except Exception as e:
        logger.warning("charts.render_failed | user_id={} | err={}", user_id, e)

    # 6) Рендер ответа
    lines: list[str] = []
    lines.append("<b>" + _("Твой план скорректирован!") + "</b>")
    lines.append("")
    if payload.goal != Goal.maintain:
        # ETA и скорость
        if new_plan.eta_date is not None and payload.goal_weight_kg is not None:
            delta = abs(payload.weight_kg - payload.goal_weight_kg)
            formatted_date = new_plan.eta_date.strftime('%d.%m.%Y')
            if payload.goal == Goal.lose:
                lines.append(f"Ты сбросишь {round(delta, 1)} кг к {formatted_date}")
            elif payload.goal == Goal.gain:
                lines.append(f"Ты наберешь {round(delta, 1)} кг к {formatted_date}")
        lines.append(f"{_('Скорость')}: {new_plan.weekly_rate_kg} {_('кг в неделю')}")
    lines.append("")
    lines.append("<b>" + _("Обновленная дневная норма:") + "</b>")
    lines.append(f"🔥 {_('Калории')}: {new_plan.calories} {_('ккал')}")
    lines.append(f"🥩 {_('Белки')}: {new_plan.protein_g} {_('г')}")
    lines.append(f"🥑 {_('Жиры')}: {new_plan.fat_g} {_('г')}")
    lines.append(f"🍞 {_('Углеводы')}: {new_plan.carbs_g} {_('г')}")
    lines.append("")
    if 'personal_line' in locals() and personal_line:
        lines.append(f"<i>{personal_line}</i>")
    lines.append(explanation)
    lines.append("")
    lines.append(_("Оставим так или нужна еще корректировка?"))

    kb = _ikb([
        [("Всё отлично!", "final:ok")],
        [("Хочу скорректировать", "final:adjust")],
    ])

    # Сначала попробуем отправить фото с подписью (единое сообщение)
    try:
        if png_data:
            caption = "\n".join(lines)
            if len(caption) <= 1024:
                await message.answer_photo(BufferedInputFile(png_data, filename="goal_plan.png"), caption=caption, reply_markup=kb)
                await state.set_state(OnboardingStates.review)
                return
            else:
                await message.answer_photo(BufferedInputFile(png_data, filename="goal_plan.png"), caption=lines[0])
                await message.answer(caption, reply_markup=kb, disable_web_page_preview=True)
                await state.set_state(OnboardingStates.review)
                return
    except Exception as e:
        logger.warning("charts.send_failed_caption | user_id={} | err={}", user_id, e)

    # Фолбэк: отправим текстом (как было), чтобы сохранить rephrase-путь
    sent_msg = await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)

    # Если включено — запустим перефраз в фоне и при успехе обновим текст сообщения
    if 'should_try_rephrase' in locals() and should_try_rephrase:
        async def _rephrase_and_edit() -> None:
            try:
                rewritten = await rephrase_explanation_cached(explanation, settings.ADJUST_REPHRASE_TONE or "neutral")
                if not rewritten:
                    logger.info("adjust.rephrase.fallback | user_id={}", user_id)
                    return
                # Сформировать обновлённый текст с перефразом
                new_lines: list[str] = []
                new_lines.append("<b>" + _("Твой план скорректирован!") + "</b>")
                new_lines.append("")
                if payload.goal != Goal.maintain:
                    if new_plan.eta_date is not None and payload.goal_weight_kg is not None:
                        delta = abs(payload.weight_kg - payload.goal_weight_kg)
                        formatted_date = new_plan.eta_date.strftime('%d.%m.%Y')
                        if payload.goal == Goal.lose:
                            new_lines.append(f"Ты сбросишь {round(delta, 1)} кг к {formatted_date}")
                        elif payload.goal == Goal.gain:
                            new_lines.append(f"Ты наберешь {round(delta, 1)} кг к {formatted_date}")
                    new_lines.append(f"{_('Скорость')}: {new_plan.weekly_rate_kg} {_('кг в неделю')}")
                new_lines.append("")
                new_lines.append("<b>" + _("Обновленная дневная норма:") + "</b>")
                new_lines.append(f"🔥 { _('Калории') }: {new_plan.calories} { _('ккал') }")
                new_lines.append(f"🥩 { _('Белки') }: {new_plan.protein_g} { _('г') }")
                new_lines.append(f"🥑 { _('Жиры') }: {new_plan.fat_g} { _('г') }")
                new_lines.append(f"🍞 { _('Углеводы') }: {new_plan.carbs_g} { _('г') }")
                new_lines.append("")
                if 'personal_line' in locals() and personal_line:
                    new_lines.append(f"<i>{personal_line}</i>")
                new_lines.append(rewritten)
                new_lines.append("")
                new_lines.append(_("Оставим так или нужна еще корректировка?"))
                try:
                    await sent_msg.edit_text("\n".join(new_lines), reply_markup=kb)
                    logger.info("adjust.rephrase.applied | user_id={} | len={}", user_id, len(rewritten))
                except Exception as e:
                    logger.warning("adjust.rephrase.edit_failed | user_id={} | err={}", user_id, e)
            except Exception as e:
                logger.warning("adjust.rephrase.error | user_id={} | err={}", user_id, e)

        asyncio.create_task(_rephrase_and_edit())

    await state.set_state(OnboardingStates.review)
