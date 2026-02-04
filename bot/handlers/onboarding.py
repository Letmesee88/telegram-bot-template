from __future__ import annotations

import asyncio
from time import perf_counter
import random
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

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
from bot.database.models import OnboardingAnswerModel, UserModel, PaymentModel
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
from bot.services.weight import save_weight
from bot.core.loader import redis_client
from bot.handlers import start as start_module
from bot.services.charts import get_plan_chart_png
from datetime import date
import hashlib
from bot.services.yookassa import create_payment
from bot.services.users import get_user_tzinfo

router = Router()

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}$')


def _onb_step_index(step_name: str) -> int | None:
    mapping = {
        "gender": 0,
        "age": 1,
        "weight": 2,
        "height": 3,
        "activity": 4,
        "goal": 5,
        "goal_weight": 6,
        "speed": 7,
        "review": 8,
        "adjust": 9,
    }
    return mapping.get(step_name)


async def _onb_mark_started(user_id: int, start_ts: int) -> None:
    try:
        await redis_client.zadd("onboarding:started", {user_id: start_ts})
        await redis_client.set(f"onboarding:start_ts:{user_id}", str(start_ts), ex=7 * 24 * 3600)
    except Exception:
        pass


async def _onb_update_last_step(user_id: int, step_name: str) -> None:
    idx = _onb_step_index(step_name)
    try:
        await redis_client.set(f"onboarding:last_step:{user_id}", step_name, ex=7 * 24 * 3600)
        if idx is not None:
            await redis_client.set(f"onboarding:last_step_index:{user_id}", str(idx), ex=7 * 24 * 3600)
    except Exception:
        pass


async def _onb_clear_redis(user_id: int) -> None:
    try:
        await redis_client.zrem("onboarding:started", user_id)
    except Exception:
        pass
    try:
        await redis_client.delete(
            f"onboarding:start_ts:{user_id}",
            f"onboarding:last_step:{user_id}",
            f"onboarding:last_step_index:{user_id}",
        )
    except Exception:
        pass


def _onb_fire_step(
    *,
    user_id: int,
    step_name: str,
    chat_id: int | None,
    chat_type: str | None,
    language: str | None,
    retry: bool | None = None,
) -> None:
    if not analytics.logger:
        return
    analytics.fire_event(
        BaseEvent(
            user_id=user_id,
            event_type="onboarding_step",
            event_properties=EventProperties(
                chat_id=chat_id,
                chat_type=chat_type,
                text=None,
                command=None,
                step_name=step_name,
                step_index=_onb_step_index(step_name),
                retry=retry,
            ),
            language=language,
            plan=Plan(branch="Onboarding", source="onboarding", version="v1"),
        )
    )


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


class EmailStates(StatesGroup):
    waiting = State()


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
        activity_text_val = data.get("activity_text")
        payload = OnboardingData(
            user_id=user_id,  
            gender=Gender(str(data["gender"])),
            age=int(data["age"]),
            weight_kg=float(data["weight_kg"]),
            height_cm=float(data["height_cm"]),
            activity_text=(str(activity_text_val) if activity_text_val is not None else None),
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
            llm_obj = await classify_activity_cached(user_id, (payload.activity_text or ""), lang_hint=getattr(message.from_user, 'language_code', None))
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
                    level = infer_activity_level(payload.activity_text or "")
            else:
                level = infer_activity_level(payload.activity_text or "")
        except Exception as e:
            logger.warning("onboarding.activity.llm_failed | user_id={} | err={}", user_id, e)
            level = infer_activity_level(payload.activity_text or "")

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
            try:
                if existing is None:
                    data_json["start_weight_kg"] = float(payload.weight_kg)
                else:
                    prev = existing.data if isinstance(getattr(existing, "data", None), dict) else {}
                    if prev.get("start_weight_kg") is None:
                        data_json["start_weight_kg"] = float(payload.weight_kg)
                    else:
                        data_json["start_weight_kg"] = prev.get("start_weight_kg")
            except Exception:
                pass
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

            # Analytics: onboarding completed (after successful commit)
            try:
                if analytics.logger:
                    d = await state.get_data()
                    started_ts = int(d.get("onboarding_started_ts") or 0)
                    completed_sent = bool(d.get("onboarding_completed_sent") is True)
                    if (started_ts > 0) and (not completed_sent):
                        now_ts = int(datetime.now(timezone.utc).timestamp())
                        total_sec = max(0, now_ts - started_ts)
                        await state.update_data(onboarding_completed_sent=True)
                        await _onb_update_last_step(user_id, "review")
                        analytics.fire_event(
                            BaseEvent(
                                user_id=user_id,
                                event_type="onboarding_completed",
                                event_properties=EventProperties(
                                    chat_id=getattr(message.chat, 'id', None),
                                    chat_type=getattr(message.chat, 'type', None),
                                    text=None,
                                    command=None,
                                    total_duration_sec=total_sec,
                                ),
                                language=getattr(message.from_user, 'language_code', None),
                                plan=Plan(branch="Onboarding", source="onboarding", version="v1"),
                            )
                        )
                        _onb_fire_step(
                            user_id=user_id,
                            step_name="review",
                            chat_id=getattr(message.chat, 'id', None),
                            chat_type=getattr(message.chat, 'type', None),
                            language=getattr(message.from_user, 'language_code', None),
                            retry=False,
                        )
                        await _onb_clear_redis(user_id)
            except Exception:
                pass
            try:
                if getattr(settings, "DAILY_REPORTS_ENABLED", True):
                    async with sessionmaker() as s2:
                        if getattr(settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False):
                            from bot.services.users import is_subscription_active
                            active = await is_subscription_active(s2, user_id)
                            if not active:
                                raise Exception("skip")
                        tz_name = await s2.scalar(select(UserModel.timezone).where(UserModel.id == user_id)) or settings.DEFAULT_TZ
                    try:
                        if (tz_name or "").upper() in ("UTC", "Z"):
                            tzinfo = timezone.utc
                        else:
                            tzinfo = ZoneInfo(tz_name)
                    except Exception:
                        tzinfo = timezone.utc
                    now_local = datetime.now(tzinfo)
                    target = datetime.combine(now_local.date(), dtime(int(getattr(settings, "DAILY_REPORTS_HOUR", 8) or 8), 0), tzinfo)
                    if now_local >= target:
                        target = target + timedelta(days=1)
                    jitter_min = int(getattr(settings, "DAILY_REPORTS_JITTER_MIN", 60) or 60)
                    target = target + timedelta(minutes=random.randint(0, max(0, jitter_min)))
                    epoch = int(target.astimezone(timezone.utc).timestamp())
                    await redis_client.zadd("reports:schedule", {user_id: epoch})
            except Exception:
                pass
            # Analytics: Adjust Applied
            try:
                if analytics.logger:
                    analytics.fire_event(
                        BaseEvent(
                            user_id=user_id,
                            event_type="Adjust:Applied",
                            event_properties=EventProperties(
                                chat_id=getattr(message.chat, 'id', None),
                                chat_type=getattr(message.chat, 'type', None),
                                text=None,
                                command=None,
                            ),
                            language=getattr(message.from_user, 'language_code', None),
                            plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                        )
                    )
            except Exception:
                pass
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
@router.callback_query(F.data == "sale:back:final")
async def sale_back_final(call: CallbackQuery, state: FSMContext) -> None:
    try:
        await _finalize_and_show(call.message, state, call.from_user.id)
    except Exception:
        pass
    await call.answer()


@router.message(EmailStates.waiting)
async def email_capture(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    # Строгая валидация формата email
    is_valid = bool(EMAIL_RE.fullmatch(raw))
    if not is_valid:
        await message.answer("Кажется, это не e-mail. Отправьте, пожалуйста, в формате: yourmail@example.ru")
        return
    try:
        async with sessionmaker() as session:
            await session.execute(update(UserModel).where(UserModel.id == message.from_user.id).values(email=raw))
            await session.commit()
    except Exception:
        await message.answer("Не удалось сохранить e-mail. Попробуйте позже.")
        await state.clear()
        return

    data = await state.get_data()
    plan = str(data.get("pay_plan") or "").strip().lower()
    if plan not in {"trial", "month", "year"}:
        await message.answer("E-mail сохранён. Теперь можно перейти к оплате.")
        await state.clear()
        return

    # Продолжаем оплату автоматически
    try:
        cp = await create_payment(user_id=message.from_user.id, plan=plan)
    except Exception:
        await message.answer("Ошибка при создании платежа. Попробуйте позже.")
        await state.clear()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("Оплатить 10 рублей" if plan == "trial" else ("Оплатить 750 руб" if plan == "month" else "Оплатить 2500 руб")), url=cp.confirmation_url)],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data=("sale:trial" if plan == "trial" else ("sale:buy:month" if plan == "month" else "sale:buy:year")))],
    ])
    await message.answer("Перейди к оплате по кнопке ниже:", reply_markup=kb, disable_web_page_preview=True)
    await state.clear()


@router.callback_query(F.data == "sale:cont1")
async def sale_cont1(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "🥗 БЖУ —  это основа для красивой фигуры\n\n"
        "Считать только калории = рыхлое тело без рельефа.\n\n"
        "🎯 Для качественного преображения придерживайся:\n"
        "• Белки: 25-30% — защищают мышцы от сжигания, надолго утоляют голод\n"
        "• Жиры: 20-25% — регулируют гормональный фон, отвечают за здоровье кожи и волос\n"
        "• Углеводы: 45-55% — обеспечивают силой для спорта и ясностью ума\n\n"
        "Пропорции можно адаптировать под свои потребности\n\n"
        "✨ Твои бонусы:\n"
        "• Упругая подтянутая фигура\n"
        "• Стабильный уровень энергии и позитивный настрой\n"
        "• Здоровая кожа и сияющие волосы\n"
        "• Никаких срывов — белок держит сытость под контролем\n\n"
        "💡 Суть: грамотный баланс БЖУ формирует не просто цифру на весах, а красоту твоего тела!"
    )
    kb = _ikb([[ ("Супер", "sale:example_food") ]])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:example_food")
async def sale_example_food(call: CallbackQuery, state: FSMContext) -> None:
    """Sales flow: show a concrete example of food photo analysis before plan selection."""
    text = (
        "Пример анализа блюда по фото\n"
        "Завтрак-ассорти с круассаном, тостами и авокадо\n\n"
        "🍜 Состав:\n"
        "• круассан (70 г, 260 ккал)\n"
        "• тост треугольники с песто (80 г, 230 ккал)\n"
        "• яичница болтунья (100 г, 180 ккал)\n"
        "• креветки жареные (60 г, 60 ккал)\n"
        "• авокадо (75 г, 120 ккал)\n"
        "• свежие овощи (огурец, листовые салаты, томаты) (60 г, 20 ккал)\n"
        "• соусы и джемы (сметана, томатный, ягодный джем) (35 г, 80 ккал)\n\n"
        "🔥 Калории: 950 ккал | 🥩 Белки: 31.5 г | 🥑 Жиры: 55.6 г | 🍞 Углеводы: 84.8 г\n\n"
        "⚖️ Вес: 430.0 г\n\n"
        "------------------------------\n\n"
        "📊 Итого за день:\n"
        "🔥 Калории: 1650 ккал (86.4% от нормы)\n"
        "🥩 Белки: 42.5 г (95.4% от нормы)\n"
        "🥑 Жиры: 55.6 г (93.9% от нормы)\n"
        "🍞 Углеводы: 99.8 г (82.5% от нормы)"
    )
    kb = _ikb([[("Отлично", "sale:cont2")]])
    try:
        photo = FSInputFile("bot/static/example_food.jpg")
        await call.message.answer_photo(photo, caption=text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:cont2")
async def sale_cont2(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "✨ Я помогу тебе достичь идеальной фигуры через:\n\n"
        "Простой учёт калорий:\n"
        "• Просто фотографируешь еду и присылаешь\n"
        "• Или пишешь текстом\n\n"
        "Личный ИИ-диетолог:\n"
        "• Ежедневно оценивает твой рацион и даёт советы\n"
        "• Подбирает аппетитные рецепты с правильным БЖУ"
    )
    # Hide trial button if user has already used trial
    used_trial = False
    try:
        async with sessionmaker() as session:
            rows = (await session.execute(
                select(PaymentModel.meta)
                .where(PaymentModel.user_id == call.from_user.id, PaymentModel.status == "succeeded")
                .order_by(PaymentModel.id.desc())
                .limit(50)
            )).scalars().all()
        for md in rows:
            try:
                if str((md or {}).get("plan", "")).lower() == "trial":
                    used_trial = True
                    break
            except Exception:
                continue
    except Exception:
        used_trial = False
    rows = []
    if not used_trial:
        rows.append([InlineKeyboardButton(text="💥 10 руб. за 3 дня", callback_data="sale:trial")])
    rows.append([InlineKeyboardButton(text="💎 Выбрать тариф", callback_data="sale:choose")])
    rows.append([InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="sale:back:final")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:trial")
async def sale_trial(call: CallbackQuery, state: FSMContext) -> None:
    # If trial already used, redirect to choose plan
    try:
        async with sessionmaker() as session:
            rows = (await session.execute(
                select(PaymentModel.meta)
                .where(PaymentModel.user_id == call.from_user.id, PaymentModel.status == "succeeded")
                .order_by(PaymentModel.id.desc())
                .limit(50)
            )).scalars().all()
        for md in rows:
            try:
                if str((md or {}).get("plan", "")).lower() == "trial":
                    await sale_choose(call, state)
                    await call.answer()
                    return
            except Exception:
                pass
    except Exception:
        pass
    # Compute trial end in user's TZ
    try:
        async with sessionmaker() as session:
            tz = await get_user_tzinfo(session, call.from_user.id)
    except Exception:
        tz = timezone.utc
    end_dt = (datetime.now(tz) + timedelta(days=3)).strftime("%d.%m.%Y %H:%M")
    text = (
        "💥 Пробный доступ всего за 10 рублей\n\n"
        "✨ 3 дня полного доступа ко всем функциям Calorissimo AI\n\n"
        "🤖 Персональный ИИ-нутрициолог\n\n"
        "📊 Анализ питания и рекомендации\n\n"
        f"• Пробный период до: {end_dt}\n\n"
        "• После пробного периода годовая подписка продлится за 2500 рублей\n\n"
        "Оплачивая, ты соглашаешься с <a href=\"https://telegra.ph/Polzovatelskoe-soglashenie-12-05-32\">Пользовательским соглашением</a>, "
        "<a href=\"https://telegra.ph/Politika-konfidencialnosti-12-05-33\">Политикой конфиденциальности</a> и на сохранение способа оплаты для автопродления.\n"
        "Автосписание можно отключить в разделе «Настройки → Подписка»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 10 рублей", callback_data="sale:pay:trial")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="sale:cont2")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:choose")
async def sale_choose(call: CallbackQuery, state: FSMContext) -> None:
    # Check if user already used trial
    used_trial = False
    try:
        async with sessionmaker() as session:
            rows = (await session.execute(
                select(PaymentModel.meta)
                .where(PaymentModel.user_id == call.from_user.id, PaymentModel.status == "succeeded")
                .order_by(PaymentModel.id.desc())
                .limit(50)
            )).scalars().all()
        for md in rows:
            try:
                if str((md or {}).get("plan", "")).lower() == "trial":
                    used_trial = True
                    break
            except Exception:
                continue
    except Exception:
        used_trial = False

    text = (
        "Выбери тариф:\n\n"
        "Месячная подписка — 750 руб/месяц\n"
        "• Ежемесячная оплата\n\n"
        "Годовая подписка — 2500 руб/в год ( или всего 210 руб/мес.)\n"
        "• Экономия 6 500 руб/ в год\n"
        "• Оплата раз в год\n\n"
        "Подписку можно отменить в любой удобный момент в Личном кабинете бота"
    )
    rows_kb = []
    if not used_trial:
        rows_kb.append([InlineKeyboardButton(text="💥 10 руб. за 3 дня", callback_data="sale:trial")])
    rows_kb.append([InlineKeyboardButton(text="750 руб/мес", callback_data="sale:buy:month")])
    rows_kb.append([InlineKeyboardButton(text="2500 руб/в год", callback_data="sale:buy:year")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:buy:month")
async def sale_buy_month(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "💎 Оплата подписки\n\n"
        "План: Месячная подписка\n"
        "Стоимость: 750 руб/месяц\n"
        "Период: 30 дней\n\n"
        "После оплаты подписка будет автоматически продлеваться.\n\n"
        "Оплачивая, ты соглашаешься с <a href=\"https://telegra.ph/Polzovatelskoe-soglashenie-12-05-32\">Пользовательским соглашением</a>, "
        "<a href=\"https://telegra.ph/Politika-konfidencialnosti-12-05-33\">Политикой конфиденциальности</a> и на сохранение способа оплаты для автопродления.\n"
        "Автосписание можно отключить в разделе «Настройки → Подписка»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 750 руб", callback_data="sale:pay:month")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="sale:choose")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:buy:year")
async def sale_buy_year(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "💎 Оплата подписки\n\n"
        "План: Годовая подписка\n"
        "Стоимость:  2500 руб/в год\n"
        "Период: 365 дней\n\n"
        "После оплаты подписка будет автоматически продлеваться.\n\n"
        "Оплачивая, ты соглашаешься с <a href=\"https://telegra.ph/Polzovatelskoe-soglashenie-12-05-32\">Пользовательским соглашением</a>, "
        "<a href=\"https://telegra.ph/Politika-konfidencialnosti-12-05-33\">Политикой конфиденциальности</a> и на сохранение способа оплаты для автопродления.\n"
        "Автосписание можно отключить в разделе «Настройки → Подписка»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 2500 руб", callback_data="sale:pay:year")],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="sale:choose")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:pay:trial")
async def sale_pay_trial(call: CallbackQuery, state: FSMContext) -> None:
    user_id = call.from_user.id
    try:
        cp = await create_payment(user_id=user_id, plan="trial")
    except Exception as e:
        if str(e) == "email_required":
            await state.set_state(EmailStates.waiting)
            try:
                await state.update_data(pay_plan="trial")
            except Exception:
                pass
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="sale:email:back")],
            ])
            await call.message.answer("🧾🙏🏼 Мы почти закончили! Нужен лишь ваш e-mail для чека. Поделитесь, пожалуйста, в формате: yourmail@example.ru", reply_markup=kb)
        elif str(e) == "trial_already_used":
            # Redirect to plan selection
            await call.message.answer("Пробный доступ доступен один раз. Выберите тариф:")
            await sale_choose(call, state)
        else:
            await call.message.answer("Ошибка при создании платежа. Попробуй позже.")
        await call.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 10 рублей", url=cp.confirmation_url)],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="sale:trial")],
    ])
    try:
        await call.message.edit_text("Перейди к оплате по кнопке ниже:", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer("Перейди к оплате по кнопке ниже:", reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:pay:month")
async def sale_pay_month(call: CallbackQuery, state: FSMContext) -> None:
    user_id = call.from_user.id
    try:
        cp = await create_payment(user_id=user_id, plan="month")
    except Exception as e:
        if str(e) == "email_required":
            await state.set_state(EmailStates.waiting)
            try:
                await state.update_data(pay_plan="month")
            except Exception:
                pass
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="sale:email:back")],
            ])
            await call.message.answer("🧾🙏🏼 Мы почти закончили! Нужен лишь ваш e-mail для чека. Поделитесь, пожалуйста, в формате: yourmail@example.ru", reply_markup=kb)
        else:
            await call.message.answer("Ошибка при создании платежа. Попробуй позже.")
        await call.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 750 руб", url=cp.confirmation_url)],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="sale:buy:month")],
    ])
    try:
        await call.message.edit_text("Перейди к оплате по кнопке ниже:", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer("Перейди к оплате по кнопке ниже:", reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:pay:year")
async def sale_pay_year(call: CallbackQuery, state: FSMContext) -> None:
    user_id = call.from_user.id
    try:
        cp = await create_payment(user_id=user_id, plan="year")
    except Exception as e:
        if str(e) == "email_required":
            await state.set_state(EmailStates.waiting)
            try:
                await state.update_data(pay_plan="year")
            except Exception:
                pass
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="sale:email:back")],
            ])
            await call.message.answer("🧾🙏🏼 Мы почти закончили! Нужен лишь ваш e-mail для чека. Поделитесь, пожалуйста, в формате: yourmail@example.ru", reply_markup=kb)
        else:
            await call.message.answer("Ошибка при создании платежа. Попробуй позже.")
        await call.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить 2500 руб", url=cp.confirmation_url)],
        [InlineKeyboardButton(text="◀️ Вернуться назад", callback_data="sale:buy:year")],
    ])
    try:
        await call.message.edit_text("Перейди к оплате по кнопке ниже:", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer("Перейди к оплате по кнопке ниже:", reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:email:back")
async def sale_email_back(call: CallbackQuery, state: FSMContext) -> None:
    """Handle back button from email request screen — return to tariff description."""
    data = await state.get_data()
    plan = str(data.get("pay_plan") or "").strip().lower()
    await state.clear()
    
    # Redirect to the appropriate tariff screen
    if plan == "trial":
        await sale_trial(call, state)
    elif plan == "month":
        await sale_buy_month(call, state)
    elif plan == "year":
        await sale_buy_year(call, state)
    else:
        # Fallback to plan selection
        await sale_choose(call, state)


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
    user_id = call.from_user.id if call.from_user else None
    await state.set_state(OnboardingStates.gender)
    if user_id is not None:
        try:
            data = await state.get_data()
            started_ts = int(data.get("onboarding_started_ts") or 0)
            if started_ts <= 0:
                started_ts = int(datetime.now(timezone.utc).timestamp())
                await state.update_data(onboarding_started_ts=started_ts, onboarding_completed_sent=False)
                await _onb_mark_started(user_id, started_ts)
                await _onb_update_last_step(user_id, "gender")
                if analytics.logger:
                    analytics.fire_event(
                        BaseEvent(
                            user_id=user_id,
                            event_type="onboarding_started",
                            event_properties=EventProperties(
                                chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                                chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                                text=None,
                                command=None,
                                source="onboarding_start",
                            ),
                            language=getattr(call.from_user, 'language_code', None),
                            plan=Plan(branch="Onboarding", source="onboarding", version="v1"),
                        )
                    )
            _onb_fire_step(
                user_id=user_id,
                step_name="gender",
                chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                language=getattr(call.from_user, 'language_code', None),
                retry=False,
            )
        except Exception:
            pass
    caption = _("Отлично! Теперь настроим всё под тебя 🎯\nПервый шаг — выбери свой пол, чтобы я точно рассчитал твою норму калорий.")
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
    caption = _("Отлично! Теперь настроим всё под тебя 🎯\nПервый шаг — выбери свой пол, чтобы я точно рассчитал твою норму калорий.")
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
    text = _("Выберите свой уровень активности. Это поможет составить максимально точный план питания. 💪🏼")
    kb = _ikb([
        [("Сидячий образ жизни", "activity:sedentary")],
        [("Активность пару раз в неделю", "activity:light")],
        [("Активность 3-4 раза в неделю", "activity:moderate")],
        [("Активность 5-6 раз в неделю", "activity:active")],
        [("Активность каждый день (7/7)", "activity:athlete")],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(OnboardingStates.activity, F.data.startswith("activity:"))
async def cb_activity_select(call: CallbackQuery, state: FSMContext) -> None:
    try:
        code = (call.data or "").split(":", 1)[1]
    except Exception:
        await call.answer()
        return
    code = (code or "").strip().lower()
    if code not in {"sedentary", "light", "moderate", "active", "athlete"}:
        await call.answer()
        return
    # Persist selection
    try:
        await state.update_data(activity_level=code)
    except Exception:
        pass
    # Remove keyboard to prevent double-clicks
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Analytics: Activity Selected
    try:
        if analytics.logger and call.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Onboarding:ActivitySelected",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                        chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, 'language_code', None),
                    plan=Plan(branch="Activity", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
    # Proceed to goal selection
    await state.set_state(OnboardingStates.goal)
    if call.from_user:
        try:
            await _onb_update_last_step(call.from_user.id, "goal")
            _onb_fire_step(
                user_id=call.from_user.id,
                step_name="goal",
                chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                language=getattr(call.from_user, 'language_code', None),
                retry=False,
            )
        except Exception:
            pass
    await _ask_goal(call.message)
    try:
        await call.answer()
    except Exception:
        pass


async def _ask_goal(message: Message) -> None:
    text = _(
        "Отлично! А теперь ключевой момент — выбираем цель ⭐️\n"
        "Calorissimo помогает достигать долгосрочных результатов благодаря точному контролю калорий"
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
        try:
            await state.update_data(goal_locked=False)
        except Exception:
            pass
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
    if call.from_user:
        try:
            await _onb_update_last_step(call.from_user.id, "age")
            _onb_fire_step(
                user_id=call.from_user.id,
                step_name="age",
                chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                language=getattr(call.from_user, 'language_code', None),
                retry=False,
            )
        except Exception:
            pass
    await call.message.answer(_("Сколько тебе лет?"))
    await call.answer()


# Текстовый fallback (male/female) — запрещаем свободный ввод, повторяем шаг с кнопками
@router.message(OnboardingStates.gender, F.text.casefold().in_(["male", "female"]))
async def gender_set(message: Message, state: FSMContext) -> None:
    caption = _("Отлично! Теперь настроим всё под тебя 🎯\nПервый шаг — выбери свой пол, чтобы я точно рассчитал твою норму калорий.")
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
    caption = _("Отлично! Теперь настроим всё под тебя 🎯\nПервый шаг — выбери свой пол, чтобы я точно рассчитал твою норму калорий.")
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
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "weight")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="weight",
                chat_id=getattr(message.chat, 'id', None),
                chat_type=getattr(message.chat, 'type', None),
                language=getattr(message.from_user, 'language_code', None),
                retry=False,
            )
        except Exception:
            pass
    await message.answer(_("Какой у тебя текущий вес в килограммах?"))


@router.message(OnboardingStates.age, F.text & (~F.text.startswith("/")))
async def age_retry(message: Message) -> None:
    if message.from_user:
        try:
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="age",
                chat_id=getattr(message.chat, 'id', None),
                chat_type=getattr(message.chat, 'type', None),
                language=getattr(message.from_user, 'language_code', None),
                retry=True,
            )
        except Exception:
            pass
    await message.answer(_("Пожалуйста, введите корректный возраст (от 1 до 120 лет)"))


@router.message(OnboardingStates.weight, F.text.regexp(r"^\d{2,3}([.,]\d{1,2})?$"))
async def weight_set(message: Message, state: FSMContext) -> None:
    w = float(message.text.replace(",", "."))
    if not (30 <= w <= 300):
        await message.answer(_("Пожалуйста, введите корректный вес (от 30 до 300 килограммов)"))
        return
    await state.update_data(weight_kg=w)
    await state.set_state(OnboardingStates.height)
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "height")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="height",
                chat_id=getattr(message.chat, 'id', None),
                chat_type=getattr(message.chat, 'type', None),
                language=getattr(message.from_user, 'language_code', None),
                retry=False,
            )
        except Exception:
            pass
    await message.answer(_("Какой у тебя рост в сантиметрах?"))


@router.message(OnboardingStates.weight, F.text & (~F.text.startswith("/")))
async def weight_retry(message: Message) -> None:
    if message.from_user:
        try:
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="weight",
                chat_id=getattr(message.chat, 'id', None),
                chat_type=getattr(message.chat, 'type', None),
                language=getattr(message.from_user, 'language_code', None),
                retry=True,
            )
        except Exception:
            pass
    await message.answer(_("Пожалуйста, введите корректный вес (от 30 до 300 килограммов)"))


@router.message(OnboardingStates.height, F.text.regexp(r"^\d{3}$"))
async def height_set(message: Message, state: FSMContext) -> None:
    h = float(message.text)
    if not (120 <= h <= 250):
        await message.answer(_("Пожалуйста, введите корректный рост (от 120 до 250 см)"))
        return
    await state.update_data(height_cm=h)
    await state.set_state(OnboardingStates.activity)
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "activity")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="activity",
                chat_id=getattr(message.chat, 'id', None),
                chat_type=getattr(message.chat, 'type', None),
                language=getattr(message.from_user, 'language_code', None),
                retry=False,
            )
        except Exception:
            pass
    await _ask_activity(message)


@router.message(OnboardingStates.height, F.text & (~F.text.startswith("/")))
async def height_retry(message: Message) -> None:
    if message.from_user:
        try:
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="height",
                chat_id=getattr(message.chat, 'id', None),
                chat_type=getattr(message.chat, 'type', None),
                language=getattr(message.from_user, 'language_code', None),
                retry=True,
            )
        except Exception:
            pass
    await message.answer(_("Пожалуйста, введите корректный рост (от 120 до 250 см)"))


@router.message(OnboardingStates.activity, F.text.len() >= 1)
async def activity_set(message: Message, state: FSMContext) -> None:
    # В новом флоу на шаге активности используем только кнопки
    await message.answer(_("Пожалуйста, используй кнопки ниже"))
    await _ask_activity(message)


@router.message(OnboardingStates.activity, F.text & (~F.text.startswith("/")))
async def activity_retry(message: Message) -> None:
    await message.answer(_("Пожалуйста, используй кнопки ниже"))
    await _ask_activity(message)


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
    logger.info("cb_goal | user_id={} | cur_state={}", getattr(call.from_user, 'id', None), cur_state)
    if cur_state != OnboardingStates.goal.state:
        # Восстановление шага выбора цели: иногда состояние смещается до клика
        try:
            await state.set_state(OnboardingStates.goal)
            await _ask_goal(call.message)
            await call.answer(_("Продублировал выбор цели — нажми кнопку ещё раз"), cache_time=3)
        except Exception:
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
    # goal_locked выставим после успешного перехода на следующий шаг

    goal_raw = call.data.split(":", 1)[1]
    await state.update_data(goal=goal_raw)

    # Снять клавиатуру немедленно, затем попытаться удалить сообщение
    try:
        await call.message.edit_reply_markup(reply_markup=None)
        logger.info("cb_goal.edit_reply_markup.ok | msg_id={}", getattr(call.message, 'message_id', None))
    except Exception as e:
        logger.warning("cb_goal.edit_reply_markup.err | user_id={} | err={}", getattr(call.from_user, 'id', None), e)
    try:
        await call.message.delete()
        logger.info("cb_goal.delete.ok | msg_id={}", getattr(call.message, 'message_id', None))
    except Exception as e:
        logger.warning("cb_goal.delete.err | user_id={} | err={}", getattr(call.from_user, 'id', None), e)

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
        if call.from_user:
            try:
                await _onb_update_last_step(call.from_user.id, "speed")
                _onb_fire_step(
                    user_id=call.from_user.id,
                    step_name="speed",
                    chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                    chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                    language=getattr(call.from_user, 'language_code', None),
                    retry=False,
                )
            except Exception:
                pass
        await state.update_data(goal_locked=True)
        await _finalize_and_show(call.message, state, call.from_user.id)
        return

    # Сразу задаём следующий вопрос отдельным фото-сообщением
    try:
        await state.set_state(OnboardingStates.goal_weight)
        logger.info("cb_goal.next_state | user_id={} | state=goal_weight", getattr(call.from_user, 'id', None))
        if call.from_user:
            try:
                await _onb_update_last_step(call.from_user.id, "goal_weight")
                _onb_fire_step(
                    user_id=call.from_user.id,
                    step_name="goal_weight",
                    chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                    chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                    language=getattr(call.from_user, 'language_code', None),
                    retry=False,
                )
            except Exception:
                pass
        try:
            photo = FSInputFile("bot/static/charts.jpg")
            await call.message.answer_photo(photo, caption=_("К какому весу ты стремишься?"))
        except Exception:
            await call.message.answer(_("К какому весу ты стремишься?"))
        await state.update_data(goal_locked=True)
        logger.info("cb_goal.ask_goal_weight.sent | user_id={}", getattr(call.from_user, 'id', None))
    except Exception as e:
        logger.exception("cb_goal.ask_goal_weight.err | user_id={} | err={}", getattr(call.from_user, 'id', None), e)
        # Снимаем лок и восстанавливаем экран цели
        try:
            await state.update_data(goal_locked=False)
            await state.set_state(OnboardingStates.goal)
            await _ask_goal(call.message)
            try:
                await call.answer(_("Повторил выбор цели"), cache_time=3)
            except Exception:
                pass
        except Exception:
            pass
        return



# Текстовый fallback цели — запрещаем свободный ввод, повторяем шаг с кнопками
@router.message(OnboardingStates.goal, F.text.casefold().in_(["lose", "gain", "maintain"]))
async def goal_set(message: Message, state: FSMContext) -> None:
    text = _(
        "Зафиксировал! Теперь самое главное — поставим цель\n"
        "Calorissimo ai помогает достигать долгосрочных результатов благодаря развитию полезных привычек"
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
        "Calorissimo ai помогает достигать долгосрочных результатов благодаря развитию полезных привычек"
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
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "speed")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="speed",
                chat_id=getattr(message.chat, 'id', None),
                chat_type=getattr(message.chat, 'type', None),
                language=getattr(message.from_user, 'language_code', None),
                retry=False,
            )
        except Exception:
            pass

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
    # Analytics: Speed Selected
    try:
        if analytics.logger and call.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Onboarding:SpeedSelected",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                        chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, 'language_code', None),
                    plan=Plan(branch="Speed", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
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
    # Если у пользователя активная платная подписка (или он админ) — вместо продаж открываем Личный кабинет
    try:
        async with sessionmaker() as session:
            from bot.database.models import UserModel  # local import to avoid circulars at module load
            from bot.services.users import is_subscription_active
            uid = call.from_user.id
            db_user = await session.get(UserModel, uid)
            is_admin = bool(getattr(db_user, "is_admin", False)) if db_user is not None else False
            active = await is_subscription_active(session, uid, include_grace=True)
        if is_admin or active:
            # Показать личный кабинет
            try:
                from bot.services.account import get_account_summary_text
                from bot.handlers.account import _kb_account  # local import to avoid cycles at module load
                text_acc = await get_account_summary_text(call.from_user.id)
                await call.message.answer(text_acc, reply_markup=_kb_account())
            except Exception:
                # Fallback: без клавиатуры
                try:
                    from bot.services.account import get_account_summary_text
                    text_acc = await get_account_summary_text(call.from_user.id)
                    await call.message.answer(text_acc)
                except Exception:
                    pass
            try:
                await state.clear()
            except Exception:
                pass
            await call.answer()
            return
    except Exception:
        # В случае ошибки — продолжаем обычный сценарий продаж
        pass

    # Гейтинг: запускаем продажи согласно ТЗ (для непремиум)
    text = (
        "💜 Секрет идеальной фигуры: считай калории\n\n"
        "Хочешь увидеть результат? Ешь меньше, чем тратишь — для похудения. Ешь больше — для набора массы. Ешь столько же — для поддержания формы. Организм сам отреагирует на твой выбор.\n\n"
        "🧮 Легкая математика: всего -200 калорий в день = -7-10 кг через год. +200 калорий = +7-10 кг набора. 0 калорий = стабильный вес\n\n"
        "✅ Почему калории — это работает:\n"
        "• Правильное питание дает 80% успеха, физ нагрузки — только 20%\n"
        "• Никаких запретов на вкусности — просто соблюдай меру\n"
        "• Безопасный метод, который не наносит урон здоровью\n"
        "• Ты сам потянешься к полезной еде — она насыщает качественнее\n\n"
        "🎯 Контроль калорий — универсальный инструмент для любой цели: похудеть, набрать массу или сохранить результат"
    )
    kb = _ikb([[ ("Продолжим", "sale:cont1") ]])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(OnboardingStates.review, F.data == "final:adjust")
async def cb_final_adjust(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.adjust)
    kb = _ikb([[("Вернуться", "final:back")]])
    await call.message.answer(_("Напиши, в свободном формате, что нужно скорректировать в твоём индивидуальном плане"), reply_markup=kb)
    # Analytics: Adjust Open
    try:
        if analytics.logger and call.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Adjust:Open",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                        chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, 'language_code', None),
                    plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
    await call.answer()


@router.callback_query(OnboardingStates.adjust, F.data == "final:back")
async def cb_final_back(call: CallbackQuery, state: FSMContext) -> None:
    # Показать финальный экран снова
    await _finalize_and_show(call.message, state, call.from_user.id)
    # Analytics: Adjust Back
    try:
        if analytics.logger and call.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Adjust:Back",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, 'id', None) if call.message else None,
                        chat_type=getattr(call.message.chat, 'type', None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, 'language_code', None),
                    plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
    await call.answer()


@router.message(OnboardingStates.adjust)
async def adjust_apply(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    text = (message.text or "").strip()
    # If user typed /start or /onboarding while in adjust, hard-redirect to start
    if text in {"/start", "/onboarding"}:
        try:
            await state.clear()
        except Exception:
            pass
        await start_module.start_handler(message, state)
        return
    # Analytics: Adjust Enter
    try:
        if analytics.logger:
            analytics.fire_event(
                BaseEvent(
                    user_id=user_id,
                    event_type="Adjust:Enter",
                    event_properties=EventProperties(
                        chat_id=getattr(message.chat, 'id', None),
                        chat_type=getattr(message.chat, 'type', None),
                        text=None,
                        command=None,
                    ),
                    language=getattr(message.from_user, 'language_code', None),
                    plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
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
                # Analytics: Adjust Fail (no onboarding data)
                try:
                    if analytics.logger:
                        analytics.fire_event(
                            BaseEvent(
                                user_id=user_id,
                                event_type="Adjust:Fail",
                                event_properties=EventProperties(
                                    chat_id=getattr(message.chat, 'id', None),
                                    chat_type=getattr(message.chat, 'type', None),
                                    text=None,
                                    command=None,
                                ),
                                language=getattr(message.from_user, 'language_code', None),
                                plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                            )
                        )
                except Exception:
                    pass
                # Redirect to fresh onboarding instead of dead-end message
                try:
                    await state.clear()
                except Exception:
                    pass
                await start_module.start_handler(message, state)
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

            # 3) Парсинг корректировки через LLM (с кешем). Ключ завязан на текущем плане, чтобы одинаковая фраза при изменившемся плане парсилась заново
            try:
                plan_key_str = f"{int(base_plan.calories)}:{int(base_plan.protein_g)}:{int(base_plan.fat_g)}:{int(base_plan.carbs_g)}"
            except Exception:
                plan_key_str = "0:0:0:0"
            try:
                plan_key = hashlib.sha256(plan_key_str.encode('utf-8')).hexdigest()[:16]
            except Exception:
                plan_key = None
            # Build base context for LLM (LLM-only mode)
            try:
                base_ctx = {
                    "base_plan": {
                        "calories": int(base_plan.calories),
                        "protein_g": int(base_plan.protein_g),
                        "fat_g": int(base_plan.fat_g),
                        "carbs_g": int(base_plan.carbs_g),
                    },
                    "goal": payload.goal.value if hasattr(payload.goal, 'value') else str(payload.goal),
                    "weight_kg": float(payload.weight_kg),
                    "goal_weight_kg": float(payload.goal_weight_kg) if payload.goal_weight_kg is not None else None,
                    "activity_level": payload.activity_level.value if hasattr(payload.activity_level, 'value') else str(payload.activity_level),
                }
            except Exception:
                base_ctx = None
            parsed = await parse_adjustment_cached(
                user_id,
                text,
                lang_hint=getattr(message.from_user, 'language_code', None),
                plan_key=plan_key,
                base_ctx=base_ctx,
            )
            if not parsed:
                # In llm_only mode, do not use local heuristics; ask user to rephrase
                if str(getattr(settings, "ADJUST_ENGINE_MODE", "")).lower() == "llm_only":
                    await message.answer(
                        _("Не до конца понял запрос. Сформулируй одной фразой, например: \n• 'уменьши углеводы на 10%' \n• 'хочу быстрее похудеть' \n• 'к 01.03.2026' \n• 'мало двигаюсь — поставь низкую активность'"))
                    return
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
        # Analytics: Adjust Fail (exception)
        try:
            if analytics.logger:
                analytics.fire_event(
                    BaseEvent(
                        user_id=getattr(message.from_user, 'id', None),
                        event_type="Adjust:Fail",
                        event_properties=EventProperties(
                            chat_id=getattr(message.chat, 'id', None),
                            chat_type=getattr(message.chat, 'type', None),
                            text=None,
                            command=None,
                        ),
                        language=getattr(message.from_user, 'language_code', None),
                        plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                    )
                )
        except Exception:
            pass
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
        # Deduplicate: skip personal line if it repeats the explanation content
        def _norm_txt(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").lower()).strip()
        pl_core = re.sub(r"^уч[её]л\s+запрос:\s*", "", personal_line, flags=re.I)
        if _norm_txt(pl_core) and _norm_txt(pl_core) not in _norm_txt(explanation):
            lines.append(personal_line)
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
                    def _norm_txt2(s: str) -> str:
                        return re.sub(r"\s+", " ", (s or "").lower()).strip()
                    pl_core2 = re.sub(r"^уч[её]л\s+запрос:\s*", "", personal_line, flags=re.I)
                    if _norm_txt2(pl_core2) and _norm_txt2(pl_core2) not in _norm_txt2(rewritten):
                        new_lines.append(personal_line)
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
