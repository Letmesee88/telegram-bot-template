from __future__ import annotations
import asyncio
import contextlib
import hashlib
import random
import re
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.i18n import gettext as _
from loguru import logger
from sqlalchemy import select, update

from bot.analytics.types import BaseEvent, EventProperties, Plan
from bot.core.config import settings
from bot.core.loader import redis_client
from bot.database.database import sessionmaker
from bot.database.models import OnboardingAnswerModel, UserModel
from bot.handlers import start as start_module
from bot.schemas.onboarding import ActivityLevel, DailyPlan, Gender, Goal, OnboardingData, Speed
from bot.services.adjust import (
    apply_adjustment,
    parse_adjustment_cached,
    parse_adjustment_heuristic,
    rephrase_explanation_cached,
)
from bot.services.analytics import analytics
from bot.services.charts import get_plan_chart_png
from bot.services.llm_activity import classify_activity_cached
from bot.services.plan import (
    SPEED_PERCENT_BY_WEIGHT,
    calculate_daily_plan,
)
from bot.services.plan import (
    _infer_activity_level as infer_activity_level,
)
from bot.services.users import get_user_tzinfo
from bot.services.subscriptions import activate_free_trial, is_free_trial_available, trial_days
from bot.services.yookassa import create_payment

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext

router = Router()

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")


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
    with contextlib.suppress(Exception):
        await redis_client.zrem("onboarding:started", user_id)
    with contextlib.suppress(Exception):
        await redis_client.delete(
            f"onboarding:start_ts:{user_id}",
            f"onboarding:last_step:{user_id}",
            f"onboarding:last_step_index:{user_id}",
        )


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
    # РџСЂРёРІРµРґРµРј Рє СѓРґРѕР±РЅРѕРјСѓ РІРёРґСѓ: 0.5, 0.75 Рё С‚.Рї.
    return (f"{val:.2f}").rstrip("0").rstrip(".")


async def _finalize_and_show(message: Message, state: FSMContext, user_id: int) -> None:
    """РЎРѕР±РёСЂР°РµС‚ payload, СЃС‡РёС‚Р°РµС‚ РїР»Р°РЅ, СЃРѕС…СЂР°РЅСЏРµС‚ РІ Р‘Р” Рё РїРѕРєР°Р·С‹РІР°РµС‚ С„РёРЅР°Р»СЊРЅС‹Р№ СЌРєСЂР°РЅ СЃ РєРЅРѕРїРєР°РјРё.
    РЈР±РёСЂР°РµРј TDEE РёР· РїРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРѕРіРѕ РІС‹РІРѕРґР° СЃРѕРіР»Р°СЃРЅРѕ РўР—.
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
        await message.answer(_("Р”Р°РЅРЅС‹Рµ РЅРµ РїСЂРѕС€Р»Рё РІР°Р»РёРґР°С†РёСЋ. РџРѕРїСЂРѕР±СѓР№ Р·Р°РЅРѕРІРѕ: /start"))
        await state.clear()
        return

    # РћРїСЂРµРґРµР»РёРј СѓСЂРѕРІРµРЅСЊ Р°РєС‚РёРІРЅРѕСЃС‚Рё. Р•СЃР»Рё СЂР°РЅРµРµ Р·Р°С„РёРєСЃРёСЂРѕРІР°Р»Рё РІ FSM вЂ” РёСЃРїРѕР»СЊР·СѓРµРј РµРіРѕ Рё РЅРµ РІС‹Р·С‹РІР°РµРј LLM РїРѕРІС‚РѕСЂРЅРѕ
    level: ActivityLevel
    llm_obj = None
    llm_used = False
    pre_level_raw = data.get("activity_level")
    if isinstance(pre_level_raw, str) and pre_level_raw in {"sedentary","light","moderate","active","athlete"}:
        level = ActivityLevel(pre_level_raw)
    else:
        try:
            llm_obj = await classify_activity_cached(user_id, (payload.activity_text or ""), lang_hint=getattr(message.from_user, "language_code", None))
            if llm_obj and isinstance(getattr(llm_obj, "level", None), str):
                lvl = (llm_obj.level or "").strip().lower()
                conf = float(getattr(llm_obj, "confidence", 0.0) or 0.0)
                if lvl in {"sedentary", "light", "moderate", "active", "athlete"} and conf >= 0.6:
                    level = ActivityLevel(lvl)
                    if level == ActivityLevel.athlete:
                        features = (getattr(llm_obj, "features", {}) or {})
                        wpw_raw = features.get("workouts_per_week")
                        wpw_num = None
                        try:
                            if isinstance(wpw_raw, (int, float)):
                                wpw_num = int(wpw_raw)
                            elif isinstance(wpw_raw, str):
                                s = wpw_raw.strip()
                                # extract first integer (supports "5вЂ“6", "5-6", "6+", "6 СЂР°Р·")
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

    # РіР°СЂР°РЅС‚РёСЂСѓРµРј, С‡С‚Рѕ СЂР°СЃС‡С‘С‚ РїР»Р°РЅР° РёСЃРїРѕР»СЊР·СѓРµС‚ РѕРїСЂРµРґРµР»С‘РЅРЅС‹Р№ СѓСЂРѕРІРµРЅСЊ
    try:
        payload = payload.model_copy(update={"activity_level": level})
    except Exception:
        try:
            payload.activity_level = level  # type: ignore[attr-defined]
        except Exception:
            pass

    plan = calculate_daily_plan(payload)

    # РЎРѕС…СЂР°РЅРµРЅРёРµ РІ Р‘Р” (upsert)
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
                with contextlib.suppress(Exception):
                    data_json["activity_llm"] = {
                        "level": getattr(llm_obj, "level", None),
                        "confidence": getattr(llm_obj, "confidence", None),
                        "features": getattr(llm_obj, "features", {}) or {},
                        "rationale": getattr(llm_obj, "rationale", None),
                        "version": getattr(llm_obj, "version", "v1"),
                    }

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
                                    chat_id=getattr(message.chat, "id", None),
                                    chat_type=getattr(message.chat, "type", None),
                                    text=None,
                                    command=None,
                                    total_duration_sec=total_sec,
                                ),
                                language=getattr(message.from_user, "language_code", None),
                                plan=Plan(branch="Onboarding", source="onboarding", version="v1"),
                            )
                        )
                        _onb_fire_step(
                            user_id=user_id,
                            step_name="review",
                            chat_id=getattr(message.chat, "id", None),
                            chat_type=getattr(message.chat, "type", None),
                            language=getattr(message.from_user, "language_code", None),
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
                                msg = "skip"
                                raise Exception(msg)
                        tz_name = await s2.scalar(select(UserModel.timezone).where(UserModel.id == user_id)) or settings.DEFAULT_TZ
                    try:
                        tzinfo = timezone.utc if (tz_name or "").upper() in ("UTC", "Z") else ZoneInfo(tz_name)
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
                                chat_id=getattr(message.chat, "id", None),
                                chat_type=getattr(message.chat, "type", None),
                                text=None,
                                command=None,
                            ),
                            language=getattr(message.from_user, "language_code", None),
                            plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                        )
                    )
            except Exception:
                pass
            logger.info("adjust.saved | user_id={} | adjustments_count={}", user_id, len(data_json.get("adjustments") or []))
    except Exception as e:
        logger.exception("onboarding.finalize.db_error | user_id={} | error={}", payload.user_id, e)
        await message.answer(_("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ РґР°РЅРЅС‹Рµ. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р· РёР»Рё РїРѕР·Р¶Рµ: /start"))
        return

    # РЎС„РѕСЂРјРёСЂРѕРІР°С‚СЊ С„РёРЅР°Р»СЊРЅС‹Р№ С‚РµРєСЃС‚ СЃРѕРіР»Р°СЃРЅРѕ РўР—
    lines: list[str] = []
    lines.append("<b>" + _("РўРІРѕР№ РёРЅРґРёРІРёРґСѓР°Р»СЊРЅС‹Р№ РїР»Р°РЅ РіРѕС‚РѕРІ!") + "</b>")
    lines.append("")

    if payload.goal != Goal.maintain:
        # ETA Рё СЃРєРѕСЂРѕСЃС‚СЊ
        if plan.eta_date is not None and payload.goal_weight_kg is not None:
            delta = abs(payload.weight_kg - payload.goal_weight_kg)
            formatted_date = plan.eta_date.strftime("%d.%m.%Y")
            if payload.goal == Goal.lose:
                lines.append(f"РўС‹ СЃР±СЂРѕСЃРёС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
            elif payload.goal == Goal.gain:
                lines.append(f"РўС‹ РЅР°Р±РµСЂРµС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
        lines.append(f"{_('РЎРєРѕСЂРѕСЃС‚СЊ')}: {plan.weekly_rate_kg} {_('РєРі РІ РЅРµРґРµР»СЋ')}")

    lines.append("")
    lines.append("<b>" + _("Р”РЅРµРІРЅР°СЏ РЅРѕСЂРјР°:") + "</b>")
    lines.append(f"рџ”Ґ {_('РљР°Р»РѕСЂРёРё')}: {plan.calories} {_('РєРєР°Р»')}")
    lines.append(f"рџҐ© {_('Р‘РµР»РєРё')}: {plan.protein_g} {_('Рі')}")
    lines.append(f"рџҐ‘ {_('Р–РёСЂС‹')}: {plan.fat_g} {_('Рі')}")
    lines.append(f"рџЌћ {_('РЈРіР»РµРІРѕРґС‹')}: {plan.carbs_g} {_('Рі')}")

    lines.append("")
    lines.append("рџ“љ <b>" + _("РќР°СѓС‡РЅС‹Рµ РѕСЃРЅРѕРІС‹ СЂР°СЃС‡РµС‚РѕРІ:") + "</b>")
    lines.append('вЂў <a href="https://pubmed.ncbi.nlm.nih.gov/2305711/">Р¤РѕСЂРјСѓР»Р° РњРёС„С„Р»РёРЅР°-РЎР°РЅ Р–РµРѕСЂР°</a>')
    lines.append('вЂў <a href="https://journals.physiology.org/doi/full/10.1152/ajpendo.00156.2017">РњРµС‚Р°Р±РѕР»РёС‡РµСЃРєРёРµ СЂР°СЃС‡РµС‚С‹</a>')
    lines.append('вЂў <a href="https://ceur-ws.org/Vol-3806/S_42_Pleskach.pdf">РЎРёСЃС‚РµРјС‹ РїРѕРґСЃС‡РµС‚Р° РєР°Р»РѕСЂРёР№</a>')

    lines.append("")
    lines.append(_("РћСЃС‚Р°РІРёРј С‚Р°Рє РёР»Рё С‡С‚Рѕ-С‚Рѕ СЃРєРѕСЂСЂРµРєС‚РёСЂСѓРµРј?"))

    kb = _ikb([
        [("РћС‚Р»РёС‡РЅРѕ", "final:ok")],
        [("РҐРѕС‡Сѓ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°С‚СЊ", "final:adjust")],
    ])

    # РџРѕРїСЂРѕР±СѓРµРј РѕС‚РїСЂР°РІРёС‚СЊ РіСЂР°С„РёРє СЃ РїРѕРґРїРёСЃСЊСЋ (РІ РёРґРµР°Р»Рµ вЂ” РІРµСЃСЊ С‚РµРєСЃС‚ РєР°Рє caption)
    try:
        if settings.CHARTS_ENABLED:
            start_w = float(payload.weight_kg)
            goal_w = float(payload.goal_weight_kg) if payload.goal_weight_kg is not None else None
            weekly = float(getattr(plan, "weekly_rate_kg", 0.0) or 0.0)
            start_dt = getattr(message, "date", None)
            start_d = start_dt.date() if start_dt else date.today()
            eta = getattr(plan, "eta_date", None)
            logger.info("charts.try_send | phase=finalize | user_id={} | weekly={} | eta={}", payload.user_id, weekly, eta)
            key_str = f"{start_w}:{goal_w}:{weekly}:{start_d.isoformat()}:{eta.isoformat() if eta else ''}:{settings.CHARTS_PRIVACY_MODE}:{settings.CHARTS_BAND_FRAC}"
            ph = hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]
            png = await get_plan_chart_png(payload.user_id, ph,
                                           start_weight=start_w,
                                           goal_weight=goal_w,
                                           weekly_rate=weekly,
                                           start_date=start_d,
                                           eta_date=eta)
            if png:
                caption = "\n".join(lines)
                # Telegram РѕРіСЂР°РЅРёС‡РёРІР°РµС‚ caption Сѓ С„РѕС‚Рѕ (~1024 СЃРёРјРІРѕР»Р°). Р•СЃР»Рё РЅРµ РїРѕРјРµС‰Р°РµС‚СЃСЏ вЂ” РѕС‚РїСЂР°РІРёРј РєРѕСЂРѕС‚РєСѓСЋ РїРѕРґРїРёСЃСЊ.
                if len(caption) <= 1024:
                    await message.answer_photo(BufferedInputFile(png, filename="goal_plan.png"), caption=caption, reply_markup=kb)
                    await state.set_state(OnboardingStates.review)
                    return
                await message.answer_photo(BufferedInputFile(png, filename="goal_plan.png"), caption=lines[0])
                await message.answer(caption, reply_markup=kb, disable_web_page_preview=True)
                await state.set_state(OnboardingStates.review)
                return
    except Exception as e:
        logger.warning("charts.send_failed_caption | user_id={} | err={}", payload.user_id, e)

    # Р¤РѕР»Р±СЌРє: РµСЃР»Рё РіСЂР°С„РёРє РѕС‚РєР»СЋС‡РµРЅ РёР»Рё РЅРµ Р·Р°РіСЂСѓР·РёР»СЃСЏ вЂ” С€Р»С‘Рј С‚РµРєСЃС‚РѕРј
    await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
    await state.set_state(OnboardingStates.review)
@router.callback_query(F.data == "sale:back:final")
async def sale_back_final(call: CallbackQuery, state: FSMContext) -> None:
    with contextlib.suppress(Exception):
        await _finalize_and_show(call.message, state, call.from_user.id)
    await call.answer()


@router.message(EmailStates.waiting)
async def email_capture(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    is_valid = bool(EMAIL_RE.fullmatch(raw))
    if not is_valid:
        await message.answer("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РѕС‚РїСЂР°РІСЊС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ e-mail РІ С„РѕСЂРјР°С‚Рµ: yourmail@example.ru")
        return

    try:
        async with sessionmaker() as session:
            await session.execute(update(UserModel).where(UserModel.id == message.from_user.id).values(email=raw))
            await session.commit()
    except Exception:
        await message.answer("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕС…СЂР°РЅРёС‚СЊ e-mail. РџРѕРїСЂРѕР±СѓР№С‚Рµ РїРѕР·Р¶Рµ.")
        await state.clear()
        return

    data = await state.get_data()
    plan = str(data.get("pay_plan") or "").strip().lower()
    if plan == "trial":
        await message.answer("Р‘РµСЃРїР»Р°С‚РЅС‹Р№ РїСЂРѕР±РЅС‹Р№ РїРµСЂРёРѕРґ Р°РєС‚РёРІРёСЂСѓРµС‚СЃСЏ Р±РµР· РѕРїР»Р°С‚С‹. Р’С‹Р±РµСЂРёС‚Рµ В«3 РґРЅСЏ Р±РµСЃРїР»Р°С‚РЅРѕВ» РІ С‚Р°СЂРёС„Р°С….")
        await state.clear()
        return

    if plan not in {"month", "year"}:
        await message.answer("E-mail СЃРѕС…СЂР°РЅРµРЅ. РњРѕР¶РЅРѕ РїРµСЂРµС…РѕРґРёС‚СЊ Рє РѕРїР»Р°С‚Рµ.")
        await state.clear()
        return

    try:
        cp = await create_payment(user_id=message.from_user.id, plan=plan)
    except Exception:
        await message.answer("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР·РґР°С‚СЊ РїР»Р°С‚РµР¶. РџРѕРїСЂРѕР±СѓР№С‚Рµ РїРѕР·Р¶Рµ.")
        await state.clear()
        return

    pay_btn = "РћРїР»Р°С‚РёС‚СЊ 750 СЂСѓР±" if plan == "month" else "РћРїР»Р°С‚РёС‚СЊ 2500 СЂСѓР±"
    back_cb = "sale:buy:month" if plan == "month" else "sale:buy:year"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=pay_btn, url=cp.confirmation_url)],
        [InlineKeyboardButton(text="РќР°Р·Р°Рґ", callback_data=back_cb)],
    ])
    await message.answer("Перейди по ссылке для оплаты:", reply_markup=kb, disable_web_page_preview=True)
    await state.clear()




@router.callback_query(F.data == "sale:cont1")
async def sale_cont1(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "рџҐ— Р‘Р–РЈ вЂ”  СЌС‚Рѕ РѕСЃРЅРѕРІР° РґР»СЏ РєСЂР°СЃРёРІРѕР№ С„РёРіСѓСЂС‹\n\n"
        "РЎС‡РёС‚Р°С‚СЊ С‚РѕР»СЊРєРѕ РєР°Р»РѕСЂРёРё = СЂС‹С…Р»РѕРµ С‚РµР»Рѕ Р±РµР· СЂРµР»СЊРµС„Р°.\n\n"
        "рџЋЇ Р”Р»СЏ РєР°С‡РµСЃС‚РІРµРЅРЅРѕРіРѕ РїСЂРµРѕР±СЂР°Р¶РµРЅРёСЏ РїСЂРёРґРµСЂР¶РёРІР°Р№СЃСЏ:\n"
        "вЂў Р‘РµР»РєРё: 25-30% вЂ” Р·Р°С‰РёС‰Р°СЋС‚ РјС‹С€С†С‹ РѕС‚ СЃР¶РёРіР°РЅРёСЏ, РЅР°РґРѕР»РіРѕ СѓС‚РѕР»СЏСЋС‚ РіРѕР»РѕРґ\n"
        "вЂў Р–РёСЂС‹: 20-25% вЂ” СЂРµРіСѓР»РёСЂСѓСЋС‚ РіРѕСЂРјРѕРЅР°Р»СЊРЅС‹Р№ С„РѕРЅ, РѕС‚РІРµС‡Р°СЋС‚ Р·Р° Р·РґРѕСЂРѕРІСЊРµ РєРѕР¶Рё Рё РІРѕР»РѕСЃ\n"
        "вЂў РЈРіР»РµРІРѕРґС‹: 45-55% вЂ” РѕР±РµСЃРїРµС‡РёРІР°СЋС‚ СЃРёР»РѕР№ РґР»СЏ СЃРїРѕСЂС‚Р° Рё СЏСЃРЅРѕСЃС‚СЊСЋ СѓРјР°\n\n"
        "РџСЂРѕРїРѕСЂС†РёРё РјРѕР¶РЅРѕ Р°РґР°РїС‚РёСЂРѕРІР°С‚СЊ РїРѕРґ СЃРІРѕРё РїРѕС‚СЂРµР±РЅРѕСЃС‚Рё\n\n"
        "вњЁ РўРІРѕРё Р±РѕРЅСѓСЃС‹:\n"
        "вЂў РЈРїСЂСѓРіР°СЏ РїРѕРґС‚СЏРЅСѓС‚Р°СЏ С„РёРіСѓСЂР°\n"
        "вЂў РЎС‚Р°Р±РёР»СЊРЅС‹Р№ СѓСЂРѕРІРµРЅСЊ СЌРЅРµСЂРіРёРё Рё РїРѕР·РёС‚РёРІРЅС‹Р№ РЅР°СЃС‚СЂРѕР№\n"
        "вЂў Р—РґРѕСЂРѕРІР°СЏ РєРѕР¶Р° Рё СЃРёСЏСЋС‰РёРµ РІРѕР»РѕСЃС‹\n"
        "вЂў РќРёРєР°РєРёС… СЃСЂС‹РІРѕРІ вЂ” Р±РµР»РѕРє РґРµСЂР¶РёС‚ СЃС‹С‚РѕСЃС‚СЊ РїРѕРґ РєРѕРЅС‚СЂРѕР»РµРј\n\n"
        "рџ’Ў РЎСѓС‚СЊ: РіСЂР°РјРѕС‚РЅС‹Р№ Р±Р°Р»Р°РЅСЃ Р‘Р–РЈ С„РѕСЂРјРёСЂСѓРµС‚ РЅРµ РїСЂРѕСЃС‚Рѕ С†РёС„СЂСѓ РЅР° РІРµСЃР°С…, Р° РєСЂР°СЃРѕС‚Сѓ С‚РІРѕРµРіРѕ С‚РµР»Р°!"
    )
    kb = _ikb([[ ("РЎСѓРїРµСЂ", "sale:example_food") ]])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:example_food")
async def sale_example_food(call: CallbackQuery, state: FSMContext) -> None:
    """Sales flow: show a concrete example of food photo analysis before plan selection."""
    text = (
        "РџСЂРёРјРµСЂ Р°РЅР°Р»РёР·Р° Р±Р»СЋРґР° РїРѕ С„РѕС‚Рѕ\n"
        "Р—Р°РІС‚СЂР°Рє-Р°СЃСЃРѕСЂС‚Рё СЃ РєСЂСѓР°СЃСЃР°РЅРѕРј, С‚РѕСЃС‚Р°РјРё Рё Р°РІРѕРєР°РґРѕ\n\n"
        "рџЌњ РЎРѕСЃС‚Р°РІ:\n"
        "вЂў РєСЂСѓР°СЃСЃР°РЅ (70 Рі, 260 РєРєР°Р»)\n"
        "вЂў С‚РѕСЃС‚ С‚СЂРµСѓРіРѕР»СЊРЅРёРєРё СЃ РїРµСЃС‚Рѕ (80 Рі, 230 РєРєР°Р»)\n"
        "вЂў СЏРёС‡РЅРёС†Р° Р±РѕР»С‚СѓРЅСЊСЏ (100 Рі, 180 РєРєР°Р»)\n"
        "вЂў РєСЂРµРІРµС‚РєРё Р¶Р°СЂРµРЅС‹Рµ (60 Рі, 60 РєРєР°Р»)\n"
        "вЂў Р°РІРѕРєР°РґРѕ (75 Рі, 120 РєРєР°Р»)\n"
        "вЂў СЃРІРµР¶РёРµ РѕРІРѕС‰Рё (РѕРіСѓСЂРµС†, Р»РёСЃС‚РѕРІС‹Рµ СЃР°Р»Р°С‚С‹, С‚РѕРјР°С‚С‹) (60 Рі, 20 РєРєР°Р»)\n"
        "вЂў СЃРѕСѓСЃС‹ Рё РґР¶РµРјС‹ (СЃРјРµС‚Р°РЅР°, С‚РѕРјР°С‚РЅС‹Р№, СЏРіРѕРґРЅС‹Р№ РґР¶РµРј) (35 Рі, 80 РєРєР°Р»)\n\n"
        "рџ”Ґ РљР°Р»РѕСЂРёРё: 950 РєРєР°Р» | рџҐ© Р‘РµР»РєРё: 31.5 Рі | рџҐ‘ Р–РёСЂС‹: 55.6 Рі | рџЌћ РЈРіР»РµРІРѕРґС‹: 84.8 Рі\n\n"
        "вљ–пёЏ Р’РµСЃ: 430.0 Рі\n\n"
        "------------------------------\n\n"
        "рџ“Љ РС‚РѕРіРѕ Р·Р° РґРµРЅСЊ:\n"
        "рџ”Ґ РљР°Р»РѕСЂРёРё: 1650 РєРєР°Р» (86.4% РѕС‚ РЅРѕСЂРјС‹)\n"
        "рџҐ© Р‘РµР»РєРё: 42.5 Рі (95.4% РѕС‚ РЅРѕСЂРјС‹)\n"
        "рџҐ‘ Р–РёСЂС‹: 55.6 Рі (93.9% РѕС‚ РЅРѕСЂРјС‹)\n"
        "рџЌћ РЈРіР»РµРІРѕРґС‹: 99.8 Рі (82.5% РѕС‚ РЅРѕСЂРјС‹)"
    )
    kb = _ikb([[("РћС‚Р»РёС‡РЅРѕ", "sale:cont2")]])
    try:
        # Remove previous text-only sales screen (sale_cont1) so we keep a single "active" screen.
        try:
            if call.message:
                await call.message.delete()
        except Exception:
            pass
        photo = FSInputFile("bot/static/example_food.jpg")
        await call.message.answer_photo(photo, caption=text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:cont2")
async def sale_cont2(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "Полный доступ к боту и всем функциям.\n\n"
        "- 3 дня бесплатного пробного периода\n"
        "- Точный AI-анализ еды по фото и БЖУ\n"
        "- Ежедневный контроль калорий и прогресса"
    )
    trial_available = False
    try:
        async with sessionmaker() as session:
            trial_available = await is_free_trial_available(session, call.from_user.id)
    except Exception as e:
        logger.warning("sale.cont2.trial_check_failed | user_id={} | err={}", call.from_user.id, e)

    rows = []
    if trial_available:
        rows.append([InlineKeyboardButton(text="3 дня бесплатно", callback_data="sale:trial")])
    rows.append([InlineKeyboardButton(text="Выбрать тариф", callback_data="sale:choose")])
    rows.append([InlineKeyboardButton(text="РќР°Р·Р°Рґ", callback_data="sale:back:final")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        msg = call.message
        if msg and (getattr(msg, "photo", None) or getattr(msg, "video", None) or getattr(msg, "animation", None) or getattr(msg, "document", None)):
            with contextlib.suppress(Exception):
                await msg.delete()
            await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)
            await call.answer()
            return
    except Exception:
        pass

    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:trial")
async def sale_trial(call: CallbackQuery, state: FSMContext) -> None:
    trial_available = False
    try:
        async with sessionmaker() as session:
            trial_available = await is_free_trial_available(session, call.from_user.id)
    except Exception as e:
        logger.warning("sale.trial.check_failed | user_id={} | err={}", call.from_user.id, e)

    if not trial_available:
        await call.message.answer("Бесплатный пробный период уже использован. Можно перейти на платный тариф.")
        await sale_choose(call, state)
        await call.answer()
        return

    try:
        async with sessionmaker() as session:
            tz = await get_user_tzinfo(session, call.from_user.id)
    except Exception:
        tz = timezone.utc

    days = trial_days()
    end_dt = (datetime.now(tz) + timedelta(days=days)).strftime("%d.%m.%Y %H:%M")
    text = (
        "Бесплатный пробный период\n\n"
        f"{days} дня доступа без оплаты.\n\n"
        f"Действует до: {end_dt}\n\n"
        "После окончания пробного периода можно выбрать платный тариф."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Активировать бесплатно", callback_data="sale:start:trial")],
        [InlineKeyboardButton(text="РќР°Р·Р°Рґ", callback_data="sale:cont2")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:choose")
async def sale_choose(call: CallbackQuery, state: FSMContext) -> None:
    trial_available = False
    try:
        async with sessionmaker() as session:
            trial_available = await is_free_trial_available(session, call.from_user.id)
    except Exception as e:
        logger.warning("sale.choose.trial_check_failed | user_id={} | err={}", call.from_user.id, e)

    text = (
        "Выбери тариф:\n\n"
        "Месяц: 750 руб / 30 дней\n"
        "Год: 2500 руб / 365 дней"
    )
    rows_kb = []
    if trial_available:
        rows_kb.append([InlineKeyboardButton(text="3 дня бесплатно", callback_data="sale:trial")])
    rows_kb.append([InlineKeyboardButton(text="750 руб / месяц", callback_data="sale:buy:month")])
    rows_kb.append([InlineKeyboardButton(text="2500 СЂСѓР± / РіРѕРґ", callback_data="sale:buy:year")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:buy:month")
async def sale_buy_month(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "Тариф Месяц\n\n"
        "План: месячный\n"
        "Стоимость: 750 руб / месяц\n"
        "Срок: 30 дней\n\n"
        "После оплаты подписка продлевается автоматически."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="РћРїР»Р°С‚РёС‚СЊ 750 СЂСѓР±", callback_data="sale:pay:month")],
        [InlineKeyboardButton(text="РќР°Р·Р°Рґ", callback_data="sale:choose")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:buy:year")
async def sale_buy_year(call: CallbackQuery, state: FSMContext) -> None:
    text = (
        "Тариф Год\n\n"
        "План: годовой\n"
        "Стоимость: 2500 руб / год\n"
        "Срок: 365 дней\n\n"
        "После оплаты подписка продлевается автоматически."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="РћРїР»Р°С‚РёС‚СЊ 2500 СЂСѓР±", callback_data="sale:pay:year")],
        [InlineKeyboardButton(text="РќР°Р·Р°Рґ", callback_data="sale:choose")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:start:trial")
@router.callback_query(F.data == "sale:pay:trial")
async def sale_start_trial(call: CallbackQuery, state: FSMContext) -> None:
    user_id = call.from_user.id
    try:
        async with sessionmaker() as session:
            expires_at_utc = await activate_free_trial(session, user_id)
            tz = await get_user_tzinfo(session, user_id)
    except Exception as e:
        logger.warning("sale.start_trial.failed | user_id={} | err={}", user_id, e)
        await call.message.answer("Пока не удалось активировать пробный период. Попробуй позже.")
        await call.answer()
        return

    if expires_at_utc is None:
        await call.message.answer("Бесплатный пробный период уже использован. Можно перейти на платный тариф.")
        await sale_choose(call, state)
        await call.answer()
        return

    end_dt = expires_at_utc.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    text = (
        "Пробный период активирован.\n\n"
        f"Действует до: {end_dt}\n\n"
        "После окончания периода выбери месяц или год."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать тариф", callback_data="sale:choose")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()




@router.callback_query(F.data == "sale:pay:month")
async def sale_pay_month(call: CallbackQuery, state: FSMContext) -> None:
    user_id = call.from_user.id
    try:
        cp = await create_payment(user_id=user_id, plan="month")
    except Exception as e:
        if str(e) == "email_required":
            await state.set_state(EmailStates.waiting)
            with contextlib.suppress(Exception):
                await state.update_data(pay_plan="month")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="в—ЂпёЏ РќР°Р·Р°Рґ", callback_data="sale:email:back")],
            ])
            await call.message.answer("рџ§ѕрџ™ЏрџЏј РњС‹ РїРѕС‡С‚Рё Р·Р°РєРѕРЅС‡РёР»Рё! РќСѓР¶РµРЅ Р»РёС€СЊ РІР°С€ e-mail РґР»СЏ С‡РµРєР°. РџРѕРґРµР»РёС‚РµСЃСЊ, РїРѕР¶Р°Р»СѓР№СЃС‚Р°, РІ С„РѕСЂРјР°С‚Рµ: yourmail@example.ru", reply_markup=kb)
        else:
            await call.message.answer("РћС€РёР±РєР° РїСЂРё СЃРѕР·РґР°РЅРёРё РїР»Р°С‚РµР¶Р°. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ.")
        await call.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="РћРїР»Р°С‚РёС‚СЊ 750 СЂСѓР±", url=cp.confirmation_url)],
        [InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="sale:buy:month")],
    ])
    try:
        await call.message.edit_text("РџРµСЂРµР№РґРё Рє РѕРїР»Р°С‚Рµ РїРѕ РєРЅРѕРїРєРµ РЅРёР¶Рµ:", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer("РџРµСЂРµР№РґРё Рє РѕРїР»Р°С‚Рµ РїРѕ РєРЅРѕРїРєРµ РЅРёР¶Рµ:", reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:pay:year")
async def sale_pay_year(call: CallbackQuery, state: FSMContext) -> None:
    user_id = call.from_user.id
    try:
        cp = await create_payment(user_id=user_id, plan="year")
    except Exception as e:
        if str(e) == "email_required":
            await state.set_state(EmailStates.waiting)
            with contextlib.suppress(Exception):
                await state.update_data(pay_plan="year")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="в—ЂпёЏ РќР°Р·Р°Рґ", callback_data="sale:email:back")],
            ])
            await call.message.answer("рџ§ѕрџ™ЏрџЏј РњС‹ РїРѕС‡С‚Рё Р·Р°РєРѕРЅС‡РёР»Рё! РќСѓР¶РµРЅ Р»РёС€СЊ РІР°С€ e-mail РґР»СЏ С‡РµРєР°. РџРѕРґРµР»РёС‚РµСЃСЊ, РїРѕР¶Р°Р»СѓР№СЃС‚Р°, РІ С„РѕСЂРјР°С‚Рµ: yourmail@example.ru", reply_markup=kb)
        else:
            await call.message.answer("РћС€РёР±РєР° РїСЂРё СЃРѕР·РґР°РЅРёРё РїР»Р°С‚РµР¶Р°. РџРѕРїСЂРѕР±СѓР№ РїРѕР·Р¶Рµ.")
        await call.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="РћРїР»Р°С‚РёС‚СЊ 2500 СЂСѓР±", url=cp.confirmation_url)],
        [InlineKeyboardButton(text="в—ЂпёЏ Р’РµСЂРЅСѓС‚СЊСЃСЏ РЅР°Р·Р°Рґ", callback_data="sale:buy:year")],
    ])
    try:
        await call.message.edit_text("РџРµСЂРµР№РґРё Рє РѕРїР»Р°С‚Рµ РїРѕ РєРЅРѕРїРєРµ РЅРёР¶Рµ:", reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer("РџРµСЂРµР№РґРё Рє РѕРїР»Р°С‚Рµ РїРѕ РєРЅРѕРїРєРµ РЅРёР¶Рµ:", reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data == "sale:email:back")
async def sale_email_back(call: CallbackQuery, state: FSMContext) -> None:
    """Handle back button from email request screen вЂ” return to tariff description."""
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
# РЎС‚Р°СЂС‚РѕРІС‹Р№ СЌРєСЂР°РЅ
# =====================

@router.message(Command("onboarding"))
@router.message(Command("onbording"))  # alias for common typo
async def cmd_onboarding(message: Message, state: FSMContext) -> None:
    logger.info("/onboarding command received -> redirect to /start | from_user={} | chat_id={}", getattr(message.from_user, "id", None), getattr(message.chat, "id", None))
    # Soft-redirect: РїРѕРєР°Р·С‹РІР°РµРј РµРґРёРЅС‹Р№ СЃС‚Р°СЂС‚РѕРІС‹Р№ СЌРєСЂР°РЅ СЃ РєРѕСЂСЂРµРєС‚РЅС‹Рј РІРµС‚РІР»РµРЅРёРµРј
    await start_module.start_handler(message, state)


# Allow launching from inline menu button (backward compat)
@router.callback_query(F.data == "onboarding")
async def cb_onboarding(call: CallbackQuery, state: FSMContext) -> None:
    logger.info("cb_onboarding | user_id={} | chat_id={}", getattr(call.from_user, "id", None), getattr(call.message.chat, "id", None))
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
                                chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                                chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                                text=None,
                                command=None,
                                source="onboarding_start",
                            ),
                            language=getattr(call.from_user, "language_code", None),
                            plan=Plan(branch="Onboarding", source="onboarding", version="v1"),
                        )
                    )
            _onb_fire_step(
                user_id=user_id,
                step_name="gender",
                chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                language=getattr(call.from_user, "language_code", None),
                retry=False,
            )
        except Exception:
            pass
    caption = _("РћС‚Р»РёС‡РЅРѕ! РўРµРїРµСЂСЊ РЅР°СЃС‚СЂРѕРёРј РІСЃС‘ РїРѕРґ С‚РµР±СЏ рџЋЇ\nРџРµСЂРІС‹Р№ С€Р°Рі вЂ” РІС‹Р±РµСЂРё СЃРІРѕР№ РїРѕР», С‡С‚РѕР±С‹ СЏ С‚РѕС‡РЅРѕ СЂР°СЃСЃС‡РёС‚Р°Р» С‚РІРѕСЋ РЅРѕСЂРјСѓ РєР°Р»РѕСЂРёР№.")
    kb = _ikb([
        [("РЇ РјСѓР¶С‡РёРЅР°", "gender:male"), ("РЇ РґРµРІСѓС€РєР°", "gender:female")],
    ])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await call.message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await call.message.answer(caption, reply_markup=kb)
    await call.answer()


# =====================
# Р’РѕР·РѕР±РЅРѕРІР»РµРЅРёРµ/РїРµСЂРµР·Р°РїСѓСЃРє РѕРЅР±РѕСЂРґРёРЅРіР°
# =====================

async def _ask_gender(message: Message) -> None:
    caption = _("РћС‚Р»РёС‡РЅРѕ! РўРµРїРµСЂСЊ РЅР°СЃС‚СЂРѕРёРј РІСЃС‘ РїРѕРґ С‚РµР±СЏ рџЋЇ\nРџРµСЂРІС‹Р№ С€Р°Рі вЂ” РІС‹Р±РµСЂРё СЃРІРѕР№ РїРѕР», С‡С‚РѕР±С‹ СЏ С‚РѕС‡РЅРѕ СЂР°СЃСЃС‡РёС‚Р°Р» С‚РІРѕСЋ РЅРѕСЂРјСѓ РєР°Р»РѕСЂРёР№.")
    kb = _ikb([[ ("РЇ РјСѓР¶С‡РёРЅР°", "gender:male"), ("РЇ РґРµРІСѓС€РєР°", "gender:female") ]])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await message.answer(caption, reply_markup=kb)


async def _ask_age(message: Message) -> None:
    await message.answer(_("РЎРєРѕР»СЊРєРѕ С‚РµР±Рµ Р»РµС‚?"))


async def _ask_weight(message: Message) -> None:
    await message.answer(_("РљР°РєРѕР№ Сѓ С‚РµР±СЏ С‚РµРєСѓС‰РёР№ РІРµСЃ РІ РєРёР»РѕРіСЂР°РјРјР°С…?"))


async def _ask_height(message: Message) -> None:
    await message.answer(_("РљР°РєРѕР№ Сѓ С‚РµР±СЏ СЂРѕСЃС‚ РІ СЃР°РЅС‚РёРјРµС‚СЂР°С…?"))


async def _ask_activity(message: Message) -> None:
    text = _("Р’С‹Р±РµСЂРёС‚Рµ СЃРІРѕР№ СѓСЂРѕРІРµРЅСЊ Р°РєС‚РёРІРЅРѕСЃС‚Рё. Р­С‚Рѕ РїРѕРјРѕР¶РµС‚ СЃРѕСЃС‚Р°РІРёС‚СЊ РјР°РєСЃРёРјР°Р»СЊРЅРѕ С‚РѕС‡РЅС‹Р№ РїР»Р°РЅ РїРёС‚Р°РЅРёСЏ. рџ’ЄрџЏј")
    kb = _ikb([
        [("РЎРёРґСЏС‡РёР№ РѕР±СЂР°Р· Р¶РёР·РЅРё", "activity:sedentary")],
        [("РђРєС‚РёРІРЅРѕСЃС‚СЊ РїР°СЂСѓ СЂР°Р· РІ РЅРµРґРµР»СЋ", "activity:light")],
        [("РђРєС‚РёРІРЅРѕСЃС‚СЊ 3-4 СЂР°Р·Р° РІ РЅРµРґРµР»СЋ", "activity:moderate")],
        [("РђРєС‚РёРІРЅРѕСЃС‚СЊ 5-6 СЂР°Р· РІ РЅРµРґРµР»СЋ", "activity:active")],
        [("РђРєС‚РёРІРЅРѕСЃС‚СЊ РєР°Р¶РґС‹Р№ РґРµРЅСЊ (7/7)", "activity:athlete")],
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
    with contextlib.suppress(Exception):
        await state.update_data(activity_level=code)
    # Remove keyboard to prevent double-clicks
    with contextlib.suppress(Exception):
        await call.message.edit_reply_markup(reply_markup=None)
    # Analytics: Activity Selected
    try:
        if analytics.logger and call.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Onboarding:ActivitySelected",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                        chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, "language_code", None),
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
                chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                language=getattr(call.from_user, "language_code", None),
                retry=False,
            )
        except Exception:
            pass
    await _ask_goal(call.message)
    with contextlib.suppress(Exception):
        await call.answer()


async def _ask_goal(message: Message) -> None:
    text = _(
        "РћС‚Р»РёС‡РЅРѕ! Рђ С‚РµРїРµСЂСЊ РєР»СЋС‡РµРІРѕР№ РјРѕРјРµРЅС‚ вЂ” РІС‹Р±РёСЂР°РµРј С†РµР»СЊ в­ђпёЏ\n"
        "Calorissimo РїРѕРјРѕРіР°РµС‚ РґРѕСЃС‚РёРіР°С‚СЊ РґРѕР»РіРѕСЃСЂРѕС‡РЅС‹С… СЂРµР·СѓР»СЊС‚Р°С‚РѕРІ Р±Р»Р°РіРѕРґР°СЂСЏ С‚РѕС‡РЅРѕРјСѓ РєРѕРЅС‚СЂРѕР»СЋ РєР°Р»РѕСЂРёР№"
    )
    kb = _ikb([
        [("РҐРѕС‡Сѓ РїРѕС…СѓРґРµС‚СЊ", "goal:lose")],
        [("РҐРѕС‡Сѓ РЅР°Р±СЂР°С‚СЊ РјС‹С€РµС‡РЅСѓСЋ РјР°СЃСЃСѓ", "goal:gain")],
        [("РҐРѕС‡Сѓ РїРѕРґРґРµСЂР¶РёРІР°С‚СЊ С‚РµРєСѓС‰РёР№ РІРµСЃ", "goal:maintain")],
    ])
    try:
        photo = FSInputFile("bot/static/charts.jpg")
        await message.answer_photo(photo, caption=text, reply_markup=kb)
    except Exception:
        await message.answer(text, reply_markup=kb)


async def _ask_goal_weight(message: Message) -> None:
    await message.answer(_("Рљ РєР°РєРѕРјСѓ РІРµСЃСѓ С‚С‹ СЃС‚СЂРµРјРёС€СЊСЃСЏ?"))


async def _ask_speed(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_w = float(data.get("weight_kg")) if data.get("weight_kg") is not None else None
    if current_w is None:
        kb_simple = _ikb([
            [("РЎ РєРѕРјС„РѕСЂС‚РѕРј", "speed:COMFORT")],
            [("РЎ СѓСЃРёР»РёРµРј", "speed:EFFORT")],
            [("РЈСЃРєРѕСЂРµРЅРЅРѕ", "speed:FAST")],
        ])
        await message.answer(_("РљР°Рє Р±С‹СЃС‚СЂРѕ С…РѕС‡РµС€СЊ РґРѕСЃС‚РёС‡СЊ С†РµР»Рё?"), reply_markup=kb_simple)
        return
    comfort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.comfort])
    effort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.effort])
    fast = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.fast])
    kb = _ikb([
        [(f"РЎ РєРѕРјС„РѕСЂС‚РѕРј {comfort} РєРі РІ РЅРµРґРµР»СЋ", "speed:COMFORT")],
        [(f"РЎ СѓСЃРёР»РёРµРј {effort} РєРі РІ РЅРµРґРµР»СЋ", "speed:EFFORT")],
        [(f"РЈСЃРєРѕСЂРµРЅРЅРѕ {fast} РєРі РІ РЅРµРґРµР»СЋ", "speed:FAST")],
    ])
    await message.answer(_("РљР°Рє Р±С‹СЃС‚СЂРѕ С…РѕС‡РµС€СЊ РґРѕСЃС‚РёС‡СЊ С†РµР»Рё?"), reply_markup=kb)


@router.callback_query(F.data == "onboarding_resume")
async def cb_onboarding_resume(call: CallbackQuery, state: FSMContext) -> None:
    # Analytics
    if analytics.logger and call.from_user:
        analytics.fire_event(
            BaseEvent(
                user_id=call.from_user.id,
                event_type="Onboarding:Resume",
                event_properties=EventProperties(
                    chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                    chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                    text=None,
                    command="/start",
                ),
                language=getattr(call.from_user, "language_code", None),
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
        with contextlib.suppress(Exception):
            await state.update_data(goal_locked=False)
        await _ask_goal(call.message)
    elif cur == OnboardingStates.goal_weight.state:
        await _ask_goal_weight(call.message)
    elif cur == OnboardingStates.speed.state:
        await _ask_speed(call.message, state)
    elif cur == OnboardingStates.review.state:
        await _finalize_and_show(call.message, state, call.from_user.id)
    elif cur == OnboardingStates.adjust.state:
        kb = _ikb([[ ("Р’РµСЂРЅСѓС‚СЊСЃСЏ", "final:back") ]])
        await call.message.answer(_("РќР°РїРёС€Рё, РІ СЃРІРѕР±РѕРґРЅРѕРј С„РѕСЂРјР°С‚Рµ, С‡С‚Рѕ РЅСѓР¶РЅРѕ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°С‚СЊ РІ С‚РІРѕС‘Рј РёРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРј РїР»Р°РЅРµ"), reply_markup=kb)
    else:
        # Fallback вЂ” РЅР°С‡РЅРµРј СЃРЅР°С‡Р°Р»Р°
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
                    chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                    chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                    text=None,
                    command="/start",
                ),
                language=getattr(call.from_user, "language_code", None),
                plan=Plan(branch="Restart", source="start", version="v1"),
            )
        )

    await state.clear()
    await cb_onboarding_start(call, state)


# =====================
# РџРѕР»
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
                chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                language=getattr(call.from_user, "language_code", None),
                retry=False,
            )
        except Exception:
            pass
    await call.message.answer(_("РЎРєРѕР»СЊРєРѕ С‚РµР±Рµ Р»РµС‚?"))
    await call.answer()


# РўРµРєСЃС‚РѕРІС‹Р№ fallback (male/female) вЂ” Р·Р°РїСЂРµС‰Р°РµРј СЃРІРѕР±РѕРґРЅС‹Р№ РІРІРѕРґ, РїРѕРІС‚РѕСЂСЏРµРј С€Р°Рі СЃ РєРЅРѕРїРєР°РјРё
@router.message(OnboardingStates.gender, F.text.casefold().in_(["male", "female"]))
async def gender_set(message: Message, state: FSMContext) -> None:
    caption = _("РћС‚Р»РёС‡РЅРѕ! РўРµРїРµСЂСЊ РЅР°СЃС‚СЂРѕРёРј РІСЃС‘ РїРѕРґ С‚РµР±СЏ рџЋЇ\nРџРµСЂРІС‹Р№ С€Р°Рі вЂ” РІС‹Р±РµСЂРё СЃРІРѕР№ РїРѕР», С‡С‚РѕР±С‹ СЏ С‚РѕС‡РЅРѕ СЂР°СЃСЃС‡РёС‚Р°Р» С‚РІРѕСЋ РЅРѕСЂРјСѓ РєР°Р»РѕСЂРёР№.")
    kb = _ikb([
        [("РЇ РјСѓР¶С‡РёРЅР°", "gender:male"), ("РЇ РґРµРІСѓС€РєР°", "gender:female")],
    ])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await message.answer(caption, reply_markup=kb)


# РќР° С€Р°РіРµ РІС‹Р±РѕСЂР° РїРѕР»Р° Р»СЋР±С‹Рµ СЃРѕРѕР±С‰РµРЅРёСЏ вЂ” С‚РѕР»СЊРєРѕ РєРЅРѕРїРєРё
@router.message(OnboardingStates.gender, F.text & (~F.text.startswith("/")))
async def gender_retry(message: Message) -> None:
    caption = _("РћС‚Р»РёС‡РЅРѕ! РўРµРїРµСЂСЊ РЅР°СЃС‚СЂРѕРёРј РІСЃС‘ РїРѕРґ С‚РµР±СЏ рџЋЇ\nРџРµСЂРІС‹Р№ С€Р°Рі вЂ” РІС‹Р±РµСЂРё СЃРІРѕР№ РїРѕР», С‡С‚РѕР±С‹ СЏ С‚РѕС‡РЅРѕ СЂР°СЃСЃС‡РёС‚Р°Р» С‚РІРѕСЋ РЅРѕСЂРјСѓ РєР°Р»РѕСЂРёР№.")
    kb = _ikb([
        [("РЇ РјСѓР¶С‡РёРЅР°", "gender:male"), ("РЇ РґРµРІСѓС€РєР°", "gender:female")],
    ])
    try:
        photo = FSInputFile("bot/static/gender.jpg")
        await message.answer_photo(photo, caption=caption, reply_markup=kb)
    except Exception:
        await message.answer(caption, reply_markup=kb)


# =====================
# Р’РѕР·СЂР°СЃС‚ / Р’РµСЃ / Р РѕСЃС‚ / РђРєС‚РёРІРЅРѕСЃС‚СЊ
# =====================

@router.message(OnboardingStates.age, F.text.regexp(r"^\d{1,3}$"))
async def age_set(message: Message, state: FSMContext) -> None:
    age = int(message.text)
    if not (1 <= age <= 120):
        await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РІРІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ РІРѕР·СЂР°СЃС‚ (РѕС‚ 1 РґРѕ 120 Р»РµС‚)"))
        return
    await state.update_data(age=age)
    await state.set_state(OnboardingStates.weight)
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "weight")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="weight",
                chat_id=getattr(message.chat, "id", None),
                chat_type=getattr(message.chat, "type", None),
                language=getattr(message.from_user, "language_code", None),
                retry=False,
            )
        except Exception:
            pass
    await message.answer(_("РљР°РєРѕР№ Сѓ С‚РµР±СЏ С‚РµРєСѓС‰РёР№ РІРµСЃ РІ РєРёР»РѕРіСЂР°РјРјР°С…?"))


@router.message(OnboardingStates.age, F.text & (~F.text.startswith("/")))
async def age_retry(message: Message) -> None:
    if message.from_user:
        with contextlib.suppress(Exception):
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="age",
                chat_id=getattr(message.chat, "id", None),
                chat_type=getattr(message.chat, "type", None),
                language=getattr(message.from_user, "language_code", None),
                retry=True,
            )
    await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РІРІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ РІРѕР·СЂР°СЃС‚ (РѕС‚ 1 РґРѕ 120 Р»РµС‚)"))


@router.message(OnboardingStates.weight, F.text.regexp(r"^\d{2,3}([.,]\d{1,2})?$"))
async def weight_set(message: Message, state: FSMContext) -> None:
    w = float(message.text.replace(",", "."))
    if not (30 <= w <= 300):
        await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РІРІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ РІРµСЃ (РѕС‚ 30 РґРѕ 300 РєРёР»РѕРіСЂР°РјРјРѕРІ)"))
        return
    await state.update_data(weight_kg=w)
    await state.set_state(OnboardingStates.height)
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "height")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="height",
                chat_id=getattr(message.chat, "id", None),
                chat_type=getattr(message.chat, "type", None),
                language=getattr(message.from_user, "language_code", None),
                retry=False,
            )
        except Exception:
            pass
    await message.answer(_("РљР°РєРѕР№ Сѓ С‚РµР±СЏ СЂРѕСЃС‚ РІ СЃР°РЅС‚РёРјРµС‚СЂР°С…?"))


@router.message(OnboardingStates.weight, F.text & (~F.text.startswith("/")))
async def weight_retry(message: Message) -> None:
    if message.from_user:
        with contextlib.suppress(Exception):
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="weight",
                chat_id=getattr(message.chat, "id", None),
                chat_type=getattr(message.chat, "type", None),
                language=getattr(message.from_user, "language_code", None),
                retry=True,
            )
    await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РІРІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ РІРµСЃ (РѕС‚ 30 РґРѕ 300 РєРёР»РѕРіСЂР°РјРјРѕРІ)"))


@router.message(OnboardingStates.height, F.text.regexp(r"^\d{3}$"))
async def height_set(message: Message, state: FSMContext) -> None:
    h = float(message.text)
    if not (120 <= h <= 250):
        await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РІРІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ СЂРѕСЃС‚ (РѕС‚ 120 РґРѕ 250 СЃРј)"))
        return
    await state.update_data(height_cm=h)
    await state.set_state(OnboardingStates.activity)
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "activity")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="activity",
                chat_id=getattr(message.chat, "id", None),
                chat_type=getattr(message.chat, "type", None),
                language=getattr(message.from_user, "language_code", None),
                retry=False,
            )
        except Exception:
            pass
    await _ask_activity(message)


@router.message(OnboardingStates.height, F.text & (~F.text.startswith("/")))
async def height_retry(message: Message) -> None:
    if message.from_user:
        with contextlib.suppress(Exception):
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="height",
                chat_id=getattr(message.chat, "id", None),
                chat_type=getattr(message.chat, "type", None),
                language=getattr(message.from_user, "language_code", None),
                retry=True,
            )
    await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РІРІРµРґРёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ СЂРѕСЃС‚ (РѕС‚ 120 РґРѕ 250 СЃРј)"))


@router.message(OnboardingStates.activity, F.text.len() >= 1)
async def activity_set(message: Message, state: FSMContext) -> None:
    # Р’ РЅРѕРІРѕРј С„Р»РѕСѓ РЅР° С€Р°РіРµ Р°РєС‚РёРІРЅРѕСЃС‚Рё РёСЃРїРѕР»СЊР·СѓРµРј С‚РѕР»СЊРєРѕ РєРЅРѕРїРєРё
    await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РёСЃРїРѕР»СЊР·СѓР№ РєРЅРѕРїРєРё РЅРёР¶Рµ"))
    await _ask_activity(message)


@router.message(OnboardingStates.activity, F.text & (~F.text.startswith("/")))
async def activity_retry(message: Message) -> None:
    await message.answer(_("РџРѕР¶Р°Р»СѓР№СЃС‚Р°, РёСЃРїРѕР»СЊР·СѓР№ РєРЅРѕРїРєРё РЅРёР¶Рµ"))
    await _ask_activity(message)


# =====================
# Р¦РµР»СЊ
# =====================

@router.callback_query(OnboardingStates.goal, F.data.startswith("goal:"))
async def cb_goal(call: CallbackQuery, state: FSMContext) -> None:
    # Р“Р°СЃРёРј СЃРїРёРЅРЅРµСЂ СЃСЂР°Р·Сѓ (РґР°Р¶Рµ РїСЂРё РїРѕРІС‚РѕСЂРЅРѕРј РєР»РёРєРµ)
    with contextlib.suppress(Exception):
        await call.answer()

    # РРґРµРјРїРѕС‚РµРЅС‚РЅС‹Р№ guard: СЃРѕСЃС‚РѕСЏРЅРёРµ Рё Р»РѕРє
    cur_state = await state.get_state()
    logger.info("cb_goal | user_id={} | cur_state={}", getattr(call.from_user, "id", None), cur_state)
    if cur_state != OnboardingStates.goal.state:
        # Р’РѕСЃСЃС‚Р°РЅРѕРІР»РµРЅРёРµ С€Р°РіР° РІС‹Р±РѕСЂР° С†РµР»Рё: РёРЅРѕРіРґР° СЃРѕСЃС‚РѕСЏРЅРёРµ СЃРјРµС‰Р°РµС‚СЃСЏ РґРѕ РєР»РёРєР°
        try:
            await state.set_state(OnboardingStates.goal)
            await _ask_goal(call.message)
            await call.answer(_("РџСЂРѕРґСѓР±Р»РёСЂРѕРІР°Р» РІС‹Р±РѕСЂ С†РµР»Рё вЂ” РЅР°Р¶РјРё РєРЅРѕРїРєСѓ РµС‰С‘ СЂР°Р·"), cache_time=3)
        except Exception:
            with contextlib.suppress(Exception):
                await call.answer(_("РЈР¶Рµ РѕР±СЂР°Р±РѕС‚Р°РЅРѕ"), cache_time=3)
        return
    data = await state.get_data()
    if data.get("goal_locked") is True:
        with contextlib.suppress(Exception):
            await call.answer(_("РЈР¶Рµ РѕР±СЂР°Р±РѕС‚Р°РЅРѕ"), cache_time=3)
        return
    # goal_locked РІС‹СЃС‚Р°РІРёРј РїРѕСЃР»Рµ СѓСЃРїРµС€РЅРѕРіРѕ РїРµСЂРµС…РѕРґР° РЅР° СЃР»РµРґСѓСЋС‰РёР№ С€Р°Рі

    goal_raw = call.data.split(":", 1)[1]
    await state.update_data(goal=goal_raw)

    # РЎРЅСЏС‚СЊ РєР»Р°РІРёР°С‚СѓСЂСѓ РЅРµРјРµРґР»РµРЅРЅРѕ, Р·Р°С‚РµРј РїРѕРїС‹С‚Р°С‚СЊСЃСЏ СѓРґР°Р»РёС‚СЊ СЃРѕРѕР±С‰РµРЅРёРµ
    try:
        await call.message.edit_reply_markup(reply_markup=None)
        logger.info("cb_goal.edit_reply_markup.ok | msg_id={}", getattr(call.message, "message_id", None))
    except Exception as e:
        logger.warning("cb_goal.edit_reply_markup.err | user_id={} | err={}", getattr(call.from_user, "id", None), e)
    try:
        await call.message.delete()
        logger.info("cb_goal.delete.ok | msg_id={}", getattr(call.message, "message_id", None))
    except Exception as e:
        logger.warning("cb_goal.delete.err | user_id={} | err={}", getattr(call.from_user, "id", None), e)

    # Analytics: РІС‹Р±РѕСЂ С†РµР»Рё
    if analytics.logger and call.from_user:
        with contextlib.suppress(Exception):
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Onboarding:GoalSelected",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                        chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, "language_code", None),
                    plan=Plan(branch="SetGoal", source="onboarding", version="v1"),
                )
            )

    if goal_raw == Goal.maintain.value:
        # Р”Р»СЏ maintain: РѕСЃС‚Р°РІР»СЏРµРј СЃРѕРѕР±С‰РµРЅРёРµ РєР°Рє РµСЃС‚СЊ, СЃСЂР°Р·Сѓ С„РёРЅР°Р»РёР·Р°С†РёСЏ
        await state.set_state(OnboardingStates.speed)
        if call.from_user:
            try:
                await _onb_update_last_step(call.from_user.id, "speed")
                _onb_fire_step(
                    user_id=call.from_user.id,
                    step_name="speed",
                    chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                    chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                    language=getattr(call.from_user, "language_code", None),
                    retry=False,
                )
            except Exception:
                pass
        await state.update_data(goal_locked=True)
        await _finalize_and_show(call.message, state, call.from_user.id)
        return

    # РЎСЂР°Р·Сѓ Р·Р°РґР°С‘Рј СЃР»РµРґСѓСЋС‰РёР№ РІРѕРїСЂРѕСЃ РѕС‚РґРµР»СЊРЅС‹Рј С„РѕС‚Рѕ-СЃРѕРѕР±С‰РµРЅРёРµРј
    try:
        await state.set_state(OnboardingStates.goal_weight)
        logger.info("cb_goal.next_state | user_id={} | state=goal_weight", getattr(call.from_user, "id", None))
        if call.from_user:
            try:
                await _onb_update_last_step(call.from_user.id, "goal_weight")
                _onb_fire_step(
                    user_id=call.from_user.id,
                    step_name="goal_weight",
                    chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                    chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                    language=getattr(call.from_user, "language_code", None),
                    retry=False,
                )
            except Exception:
                pass
        try:
            photo = FSInputFile("bot/static/charts.jpg")
            await call.message.answer_photo(photo, caption=_("Рљ РєР°РєРѕРјСѓ РІРµСЃСѓ С‚С‹ СЃС‚СЂРµРјРёС€СЊСЃСЏ?"))
        except Exception:
            await call.message.answer(_("Рљ РєР°РєРѕРјСѓ РІРµСЃСѓ С‚С‹ СЃС‚СЂРµРјРёС€СЊСЃСЏ?"))
        await state.update_data(goal_locked=True)
        logger.info("cb_goal.ask_goal_weight.sent | user_id={}", getattr(call.from_user, "id", None))
    except Exception as e:
        logger.exception("cb_goal.ask_goal_weight.err | user_id={} | err={}", getattr(call.from_user, "id", None), e)
        # РЎРЅРёРјР°РµРј Р»РѕРє Рё РІРѕСЃСЃС‚Р°РЅР°РІР»РёРІР°РµРј СЌРєСЂР°РЅ С†РµР»Рё
        try:
            await state.update_data(goal_locked=False)
            await state.set_state(OnboardingStates.goal)
            await _ask_goal(call.message)
            with contextlib.suppress(Exception):
                await call.answer(_("РџРѕРІС‚РѕСЂРёР» РІС‹Р±РѕСЂ С†РµР»Рё"), cache_time=3)
        except Exception:
            pass
        return



# РўРµРєСЃС‚РѕРІС‹Р№ fallback С†РµР»Рё вЂ” Р·Р°РїСЂРµС‰Р°РµРј СЃРІРѕР±РѕРґРЅС‹Р№ РІРІРѕРґ, РїРѕРІС‚РѕСЂСЏРµРј С€Р°Рі СЃ РєРЅРѕРїРєР°РјРё
@router.message(OnboardingStates.goal, F.text.casefold().in_(["lose", "gain", "maintain"]))
async def goal_set(message: Message, state: FSMContext) -> None:
    text = _(
        "Р—Р°С„РёРєСЃРёСЂРѕРІР°Р»! РўРµРїРµСЂСЊ СЃР°РјРѕРµ РіР»Р°РІРЅРѕРµ вЂ” РїРѕСЃС‚Р°РІРёРј С†РµР»СЊ\n"
        "Calorissimo ai РїРѕРјРѕРіР°РµС‚ РґРѕСЃС‚РёРіР°С‚СЊ РґРѕР»РіРѕСЃСЂРѕС‡РЅС‹С… СЂРµР·СѓР»СЊС‚Р°С‚РѕРІ Р±Р»Р°РіРѕРґР°СЂСЏ СЂР°Р·РІРёС‚РёСЋ РїРѕР»РµР·РЅС‹С… РїСЂРёРІС‹С‡РµРє"
    )
    kb = _ikb([
        [("РҐРѕС‡Сѓ РїРѕС…СѓРґРµС‚СЊ", "goal:lose")],
        [("РҐРѕС‡Сѓ РЅР°Р±СЂР°С‚СЊ РјС‹С€РµС‡РЅСѓСЋ РјР°СЃСЃСѓ", "goal:gain")],
        [("РҐРѕС‡Сѓ РїРѕРґРґРµСЂР¶РёРІР°С‚СЊ С‚РµРєСѓС‰РёР№ РІРµСЃ", "goal:maintain")],
    ])
    await message.answer(text, reply_markup=kb)


@router.message(OnboardingStates.goal, F.text & (~F.text.startswith("/")))
async def goal_retry(message: Message) -> None:
    text = _(
        "Р—Р°С„РёРєСЃРёСЂРѕРІР°Р»! РўРµРїРµСЂСЊ СЃР°РјРѕРµ РіР»Р°РІРЅРѕРµ вЂ” РїРѕСЃС‚Р°РІРёРј С†РµР»СЊ\n"
        "Calorissimo ai РїРѕРјРѕРіР°РµС‚ РґРѕСЃС‚РёРіР°С‚СЊ РґРѕР»РіРѕСЃСЂРѕС‡РЅС‹С… СЂРµР·СѓР»СЊС‚Р°С‚РѕРІ Р±Р»Р°РіРѕРґР°СЂСЏ СЂР°Р·РІРёС‚РёСЋ РїРѕР»РµР·РЅС‹С… РїСЂРёРІС‹С‡РµРє"
    )
    kb = _ikb([
        [("РҐРѕС‡Сѓ РїРѕС…СѓРґРµС‚СЊ", "goal:lose")],
        [("РҐРѕС‡Сѓ РЅР°Р±СЂР°С‚СЊ РјС‹С€РµС‡РЅСѓСЋ РјР°СЃСЃСѓ", "goal:gain")],
        [("РҐРѕС‡Сѓ РїРѕРґРґРµСЂР¶РёРІР°С‚СЊ С‚РµРєСѓС‰РёР№ РІРµСЃ", "goal:maintain")],
    ])
    await message.answer(text, reply_markup=kb)


# =====================
# Р¦РµР»РµРІРѕР№ РІРµСЃ -> РєРЅРѕРїРєРё СЃРєРѕСЂРѕСЃС‚Рё
# =====================

@router.message(OnboardingStates.goal_weight, F.text.regexp(r"^\d{2,3}([.,]\d{1,2})?$"))
async def goal_weight_set(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_w = float(data.get("weight_kg"))
    goal_w = float(message.text.replace(",", "."))
    goal_raw = str(data.get("goal"))

    # Р‘РёР·РЅРµСЃ-РІР°Р»РёРґР°С†РёСЏ
    if goal_raw == "lose" and goal_w >= current_w:
        await message.answer(_("Р”Р»СЏ РїРѕС…СѓРґРµРЅРёСЏ С†РµР»РµРІРѕР№ РІРµСЃ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ РјРµРЅСЊС€Рµ С‚РµРєСѓС‰РµРіРѕ. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р·."))
        return
    if goal_raw == "gain" and goal_w <= current_w:
        await message.answer(_("Р”Р»СЏ РЅР°Р±РѕСЂР° РјР°СЃСЃС‹ С†РµР»РµРІРѕР№ РІРµСЃ РґРѕР»Р¶РµРЅ Р±С‹С‚СЊ Р±РѕР»СЊС€Рµ С‚РµРєСѓС‰РµРіРѕ. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р·."))
        return

    await state.update_data(goal_weight_kg=goal_w)
    await state.set_state(OnboardingStates.speed)
    if message.from_user:
        try:
            await _onb_update_last_step(message.from_user.id, "speed")
            _onb_fire_step(
                user_id=message.from_user.id,
                step_name="speed",
                chat_id=getattr(message.chat, "id", None),
                chat_type=getattr(message.chat, "type", None),
                language=getattr(message.from_user, "language_code", None),
                retry=False,
            )
        except Exception:
            pass

    # Р”РёРЅР°РјРёС‡РµСЃРєРёРµ N РєРі/РЅРµРґ
    comfort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.comfort])
    effort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.effort])
    fast = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.fast])

    kb = _ikb([
        [(f"РЎ РєРѕРјС„РѕСЂС‚РѕРј {comfort} РєРі РІ РЅРµРґРµР»СЋ", "speed:COMFORT")],
        [(f"РЎ СѓСЃРёР»РёРµРј {effort} РєРі РІ РЅРµРґРµР»СЋ", "speed:EFFORT")],
        [(f"РЈСЃРєРѕСЂРµРЅРЅРѕ {fast} РєРі РІ РЅРµРґРµР»СЋ", "speed:FAST")],
    ])
    await message.answer(_("РљР°Рє Р±С‹СЃС‚СЂРѕ С…РѕС‡РµС€СЊ РґРѕСЃС‚РёС‡СЊ С†РµР»Рё?"), reply_markup=kb)


@router.message(OnboardingStates.goal_weight, F.text & (~F.text.startswith("/")))
async def goal_weight_retry(message: Message) -> None:
    await message.answer(_("РќРµРєРѕСЂСЂРµРєС‚РЅС‹Р№ С„РѕСЂРјР°С‚. РџСЂРёРјРµСЂ: 75.0"))


# =====================
# Р’С‹Р±РѕСЂ СЃРєРѕСЂРѕСЃС‚Рё (РєРЅРѕРїРєРё) -> С„РёРЅР°Р»РёР·Р°С†РёСЏ
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
                        chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                        chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, "language_code", None),
                    plan=Plan(branch="Speed", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
    await _finalize_and_show(call.message, state, call.from_user.id)
    await call.answer()


# Fallback: РІРІРѕРґ СЃРєРѕСЂРѕСЃС‚Рё С‚РµРєСЃС‚РѕРј вЂ” Р·Р°РїСЂРµС‰Р°РµРј СЃРІРѕР±РѕРґРЅС‹Р№ РІРІРѕРґ, РїРѕРІС‚РѕСЂСЏРµРј С€Р°Рі СЃ РєРЅРѕРїРєР°РјРё
@router.message(OnboardingStates.speed, F.text & (~F.text.startswith("/")))
async def speed_and_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    current_w = float(data.get("weight_kg")) if data.get("weight_kg") is not None else None

    # Р•СЃР»Рё РЅРµС‚ РІРµСЃР° РІ СЃРѕСЃС‚РѕСЏРЅРёРё, РїСЂРѕСЃС‚Рѕ РїСЂРѕСЃРёРј РІС‹Р±СЂР°С‚СЊ РєРЅРѕРїРєСѓ РµС‰С‘ СЂР°Р·
    if current_w is None:
        kb = _ikb([
            [("РЎ РєРѕРјС„РѕСЂС‚РѕРј", "speed:COMFORT")],
            [("РЎ СѓСЃРёР»РёРµРј", "speed:EFFORT")],
            [("РЈСЃРєРѕСЂРµРЅРЅРѕ", "speed:FAST")],
        ])
        await message.answer(_("РљР°Рє Р±С‹СЃС‚СЂРѕ С…РѕС‡РµС€СЊ РґРѕСЃС‚РёС‡СЊ С†РµР»Рё?"), reply_markup=kb)
        return

    comfort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.comfort])
    effort = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.effort])
    fast = _format_rate(current_w, SPEED_PERCENT_BY_WEIGHT[Speed.fast])

    kb = _ikb([
        [(f"РЎ РєРѕРјС„РѕСЂС‚РѕРј {comfort} РєРі РІ РЅРµРґРµР»СЋ", "speed:COMFORT")],
        [(f"РЎ СѓСЃРёР»РёРµРј {effort} РєРі РІ РЅРµРґРµР»СЋ", "speed:EFFORT")],
        [(f"РЈСЃРєРѕСЂРµРЅРЅРѕ {fast} РєРі РІ РЅРµРґРµР»СЋ", "speed:FAST")],
    ])
    await message.answer(_("РљР°Рє Р±С‹СЃС‚СЂРѕ С…РѕС‡РµС€СЊ РґРѕСЃС‚РёС‡СЊ С†РµР»Рё?"), reply_markup=kb)

# =====================
# Р¤РёРЅР°Р»СЊРЅС‹Р№ СЌРєСЂР°РЅ: OK / Adjust
# =====================

@router.callback_query(OnboardingStates.review, F.data == "final:ok")
async def cb_final_ok(call: CallbackQuery, state: FSMContext) -> None:
    # Р•СЃР»Рё Сѓ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ Р°РєС‚РёРІРЅР°СЏ РїР»Р°С‚РЅР°СЏ РїРѕРґРїРёСЃРєР° (РёР»Рё РѕРЅ Р°РґРјРёРЅ) вЂ” РІРјРµСЃС‚Рѕ РїСЂРѕРґР°Р¶ РѕС‚РєСЂС‹РІР°РµРј Р›РёС‡РЅС‹Р№ РєР°Р±РёРЅРµС‚
    try:
        async with sessionmaker() as session:
            from bot.database.models import UserModel  # local import to avoid circulars at module load
            from bot.services.users import is_subscription_active
            uid = call.from_user.id
            db_user = await session.get(UserModel, uid)
            is_admin = bool(getattr(db_user, "is_admin", False)) if db_user is not None else False
            active = await is_subscription_active(session, uid, include_grace=True)
        if is_admin or active:
            # РџРѕРєР°Р·Р°С‚СЊ Р»РёС‡РЅС‹Р№ РєР°Р±РёРЅРµС‚
            try:
                from bot.handlers.account import _kb_account  # local import to avoid cycles at module load
                from bot.services.account import get_account_summary_text
                text_acc = await get_account_summary_text(call.from_user.id)
                await call.message.answer(text_acc, reply_markup=_kb_account())
            except Exception:
                # Fallback: Р±РµР· РєР»Р°РІРёР°С‚СѓСЂС‹
                try:
                    from bot.services.account import get_account_summary_text
                    text_acc = await get_account_summary_text(call.from_user.id)
                    await call.message.answer(text_acc)
                except Exception:
                    pass
            with contextlib.suppress(Exception):
                await state.clear()
            await call.answer()
            return
    except Exception:
        # Р’ СЃР»СѓС‡Р°Рµ РѕС€РёР±РєРё вЂ” РїСЂРѕРґРѕР»Р¶Р°РµРј РѕР±С‹С‡РЅС‹Р№ СЃС†РµРЅР°СЂРёР№ РїСЂРѕРґР°Р¶
        pass

    # Р“РµР№С‚РёРЅРі: Р·Р°РїСѓСЃРєР°РµРј РїСЂРѕРґР°Р¶Рё СЃРѕРіР»Р°СЃРЅРѕ РўР— (РґР»СЏ РЅРµРїСЂРµРјРёСѓРј)
    text = (
        "рџ’њ РЎРµРєСЂРµС‚ РёРґРµР°Р»СЊРЅРѕР№ С„РёРіСѓСЂС‹: СЃС‡РёС‚Р°Р№ РєР°Р»РѕСЂРёРё\n\n"
        "РҐРѕС‡РµС€СЊ СѓРІРёРґРµС‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚? Р•С€СЊ РјРµРЅСЊС€Рµ, С‡РµРј С‚СЂР°С‚РёС€СЊ вЂ” РґР»СЏ РїРѕС…СѓРґРµРЅРёСЏ. Р•С€СЊ Р±РѕР»СЊС€Рµ вЂ” РґР»СЏ РЅР°Р±РѕСЂР° РјР°СЃСЃС‹. Р•С€СЊ СЃС‚РѕР»СЊРєРѕ Р¶Рµ вЂ” РґР»СЏ РїРѕРґРґРµСЂР¶Р°РЅРёСЏ С„РѕСЂРјС‹. РћСЂРіР°РЅРёР·Рј СЃР°Рј РѕС‚СЂРµР°РіРёСЂСѓРµС‚ РЅР° С‚РІРѕР№ РІС‹Р±РѕСЂ.\n\n"
        "рџ§® Р›РµРіРєР°СЏ РјР°С‚РµРјР°С‚РёРєР°: РІСЃРµРіРѕ -200 РєР°Р»РѕСЂРёР№ РІ РґРµРЅСЊ = -7-10 РєРі С‡РµСЂРµР· РіРѕРґ. +200 РєР°Р»РѕСЂРёР№ = +7-10 РєРі РЅР°Р±РѕСЂР°. 0 РєР°Р»РѕСЂРёР№ = СЃС‚Р°Р±РёР»СЊРЅС‹Р№ РІРµСЃ\n\n"
        "вњ… РџРѕС‡РµРјСѓ РєР°Р»РѕСЂРёРё вЂ” СЌС‚Рѕ СЂР°Р±РѕС‚Р°РµС‚:\n"
        "вЂў РџСЂР°РІРёР»СЊРЅРѕРµ РїРёС‚Р°РЅРёРµ РґР°РµС‚ 80% СѓСЃРїРµС…Р°, С„РёР· РЅР°РіСЂСѓР·РєРё вЂ” С‚РѕР»СЊРєРѕ 20%\n"
        "вЂў РќРёРєР°РєРёС… Р·Р°РїСЂРµС‚РѕРІ РЅР° РІРєСѓСЃРЅРѕСЃС‚Рё вЂ” РїСЂРѕСЃС‚Рѕ СЃРѕР±Р»СЋРґР°Р№ РјРµСЂСѓ\n"
        "вЂў Р‘РµР·РѕРїР°СЃРЅС‹Р№ РјРµС‚РѕРґ, РєРѕС‚РѕСЂС‹Р№ РЅРµ РЅР°РЅРѕСЃРёС‚ СѓСЂРѕРЅ Р·РґРѕСЂРѕРІСЊСЋ\n"
        "вЂў РўС‹ СЃР°Рј РїРѕС‚СЏРЅРµС€СЊСЃСЏ Рє РїРѕР»РµР·РЅРѕР№ РµРґРµ вЂ” РѕРЅР° РЅР°СЃС‹С‰Р°РµС‚ РєР°С‡РµСЃС‚РІРµРЅРЅРµРµ\n\n"
        "рџЋЇ РљРѕРЅС‚СЂРѕР»СЊ РєР°Р»РѕСЂРёР№ вЂ” СѓРЅРёРІРµСЂСЃР°Р»СЊРЅС‹Р№ РёРЅСЃС‚СЂСѓРјРµРЅС‚ РґР»СЏ Р»СЋР±РѕР№ С†РµР»Рё: РїРѕС…СѓРґРµС‚СЊ, РЅР°Р±СЂР°С‚СЊ РјР°СЃСЃСѓ РёР»Рё СЃРѕС…СЂР°РЅРёС‚СЊ СЂРµР·СѓР»СЊС‚Р°С‚"
    )
    kb = _ikb([[ ("РџСЂРѕРґРѕР»Р¶РёРј", "sale:cont1") ]])
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(OnboardingStates.review, F.data == "final:adjust")
async def cb_final_adjust(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.adjust)
    kb = _ikb([[("Р’РµСЂРЅСѓС‚СЊСЃСЏ", "final:back")]])
    await call.message.answer(_("РќР°РїРёС€Рё, РІ СЃРІРѕР±РѕРґРЅРѕРј С„РѕСЂРјР°С‚Рµ, С‡С‚Рѕ РЅСѓР¶РЅРѕ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°С‚СЊ РІ С‚РІРѕС‘Рј РёРЅРґРёРІРёРґСѓР°Р»СЊРЅРѕРј РїР»Р°РЅРµ"), reply_markup=kb)
    # Analytics: Adjust Open
    try:
        if analytics.logger and call.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Adjust:Open",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                        chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, "language_code", None),
                    plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
    await call.answer()


@router.callback_query(OnboardingStates.adjust, F.data == "final:back")
async def cb_final_back(call: CallbackQuery, state: FSMContext) -> None:
    # РџРѕРєР°Р·Р°С‚СЊ С„РёРЅР°Р»СЊРЅС‹Р№ СЌРєСЂР°РЅ СЃРЅРѕРІР°
    await _finalize_and_show(call.message, state, call.from_user.id)
    # Analytics: Adjust Back
    try:
        if analytics.logger and call.from_user:
            analytics.fire_event(
                BaseEvent(
                    user_id=call.from_user.id,
                    event_type="Adjust:Back",
                    event_properties=EventProperties(
                        chat_id=getattr(call.message.chat, "id", None) if call.message else None,
                        chat_type=getattr(call.message.chat, "type", None) if call.message else None,
                        text=None,
                        command=None,
                    ),
                    language=getattr(call.from_user, "language_code", None),
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
        with contextlib.suppress(Exception):
            await state.clear()
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
                        chat_id=getattr(message.chat, "id", None),
                        chat_type=getattr(message.chat, "type", None),
                        text=None,
                        command=None,
                    ),
                    language=getattr(message.from_user, "language_code", None),
                    plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                )
            )
    except Exception:
        pass
    # Immediate UX feedback while we process
    with contextlib.suppress(Exception):
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    await message.answer(_("вњЁ РР·СѓС‡Р°СЋ РІР°С€Рё РїРѕР¶РµР»Р°РЅРёСЏ Рё РѕР±РЅРѕРІР»СЏСЋ РїР»Р°РЅ..."))
    logger.info(
        "adjust.enter | user_id={} | text_len={} | state=OnboardingStates.adjust",
        user_id,
        len(text),
    )

    # 1) РџРѕР»СѓС‡Р°РµРј РїРѕСЃР»РµРґРЅСЋСЋ Р·Р°РїРёСЃСЊ РѕРЅР±РѕСЂРґРёРЅРіР°
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
                                    chat_id=getattr(message.chat, "id", None),
                                    chat_type=getattr(message.chat, "type", None),
                                    text=None,
                                    command=None,
                                ),
                                language=getattr(message.from_user, "language_code", None),
                                plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                            )
                        )
                except Exception:
                    pass
                # Redirect to fresh onboarding instead of dead-end message
                with contextlib.suppress(Exception):
                    await state.clear()
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

            # 2) Р’РѕСЃСЃС‚Р°РЅРѕРІРёРј РѕР±СЉРµРєС‚С‹ РґР»СЏ РІС‹С‡РёСЃР»РµРЅРёР№
            try:
                base_plan_dict = data_json.get("base_plan") or dp_json
                base_plan = DailyPlan.model_validate(base_plan_dict)
            except Exception:
                # Р¤РѕР»Р±СЌРє: СЃРѕР±РµСЂС‘Рј base_plan РёР· current
                base_plan = DailyPlan.model_validate(dp_json)
                data_json["base_plan"] = base_plan.model_dump(mode="json")

            try:
                payload = OnboardingData.model_validate(data_json)
            except Exception as e:
                logger.warning("adjust.payload_invalid | user_id={} | err={}", user_id, e)
                await message.answer(_("Р”Р°РЅРЅС‹Рµ РѕРЅР±РѕСЂРґРёРЅРіР° РїРѕРІСЂРµР¶РґРµРЅС‹. РџРѕРїСЂРѕР±СѓР№ Р·Р°РЅРѕРІРѕ: /start"))
                return

            # 3) РџР°СЂСЃРёРЅРі РєРѕСЂСЂРµРєС‚РёСЂРѕРІРєРё С‡РµСЂРµР· LLM (СЃ РєРµС€РµРј). РљР»СЋС‡ Р·Р°РІСЏР·Р°РЅ РЅР° С‚РµРєСѓС‰РµРј РїР»Р°РЅРµ, С‡С‚РѕР±С‹ РѕРґРёРЅР°РєРѕРІР°СЏ С„СЂР°Р·Р° РїСЂРё РёР·РјРµРЅРёРІС€РµРјСЃСЏ РїР»Р°РЅРµ РїР°СЂСЃРёР»Р°СЃСЊ Р·Р°РЅРѕРІРѕ
            try:
                plan_key_str = f"{int(base_plan.calories)}:{int(base_plan.protein_g)}:{int(base_plan.fat_g)}:{int(base_plan.carbs_g)}"
            except Exception:
                plan_key_str = "0:0:0:0"
            try:
                plan_key = hashlib.sha256(plan_key_str.encode("utf-8")).hexdigest()[:16]
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
                    "goal": payload.goal.value if hasattr(payload.goal, "value") else str(payload.goal),
                    "weight_kg": float(payload.weight_kg),
                    "goal_weight_kg": float(payload.goal_weight_kg) if payload.goal_weight_kg is not None else None,
                    "activity_level": payload.activity_level.value if hasattr(payload.activity_level, "value") else str(payload.activity_level),
                }
            except Exception:
                base_ctx = None
            parsed = await parse_adjustment_cached(
                user_id,
                text,
                lang_hint=getattr(message.from_user, "language_code", None),
                plan_key=plan_key,
                base_ctx=base_ctx,
            )
            if not parsed:
                # In llm_only mode, do not use local heuristics; ask user to rephrase
                if str(getattr(settings, "ADJUST_ENGINE_MODE", "")).lower() == "llm_only":
                    await message.answer(
                        _("РќРµ РґРѕ РєРѕРЅС†Р° РїРѕРЅСЏР» Р·Р°РїСЂРѕСЃ. РЎС„РѕСЂРјСѓР»РёСЂСѓР№ РѕРґРЅРѕР№ С„СЂР°Р·РѕР№, РЅР°РїСЂРёРјРµСЂ: \nвЂў 'СѓРјРµРЅСЊС€Рё СѓРіР»РµРІРѕРґС‹ РЅР° 10%' \nвЂў 'С…РѕС‡Сѓ Р±С‹СЃС‚СЂРµРµ РїРѕС…СѓРґРµС‚СЊ' \nвЂў 'Рє 01.03.2026' \nвЂў 'РјР°Р»Рѕ РґРІРёРіР°СЋСЃСЊ вЂ” РїРѕСЃС‚Р°РІСЊ РЅРёР·РєСѓСЋ Р°РєС‚РёРІРЅРѕСЃС‚СЊ'"))
                    return
                # Heuristic fallback for top intents (offline, RU)
                h = parse_adjustment_heuristic(text)
                if h:
                    logger.info("adjust.heuristic_used | user_id={} | intents={} | text_len={}", user_id, h.intents, len(text))
                    parsed = h
                else:
                    await message.answer(
                        _("РќРµ РґРѕ РєРѕРЅС†Р° РїРѕРЅСЏР» Р·Р°РїСЂРѕСЃ. РЎС„РѕСЂРјСѓР»РёСЂСѓР№ РѕРґРЅРѕР№ С„СЂР°Р·РѕР№, РЅР°РїСЂРёРјРµСЂ: \nвЂў 'СѓР±РµСЂРёС‚Рµ СѓРіР»РµРІРѕРґС‹' \nвЂў 'РґРѕР±Р°РІСЊ 200 РєРєР°Р»' \nвЂў 'РјР°Р»Рѕ РґРІРёРіР°СЋСЃСЊ вЂ” РїРѕСЃС‚Р°РІСЊ РЅРёР·РєСѓСЋ Р°РєС‚РёРІРЅРѕСЃС‚СЊ'"))
                    return
            else:
                logger.info(
                    "adjust.parsed | user_id={} | intents={} | activity_override={} | calories={} | macros={} | conf={}",
                    user_id,
                    getattr(parsed, "intents", None),
                    getattr(parsed, "activity_override", None),
                    getattr(parsed, "calories", None),
                    getattr(parsed, "macros", None),
                    getattr(parsed, "confidence", None),
                )

            # 4) РџСЂРёРјРµРЅРёРј РґРµС‚РµСЂРјРёРЅРёСЂРѕРІР°РЅРЅС‹Рµ РїСЂР°РІРёР»Р°
            new_plan, explanation, summary = apply_adjustment(base_plan, payload, parsed)
            logger.info(
                "adjust.applied | user_id={} | calories={} | p/f/c={}/{}/{}",
                user_id,
                new_plan.calories,
                new_plan.protein_g,
                new_plan.fat_g,
                new_plan.carbs_g,
            )

            # РЎС„РѕСЂРјРёСЂСѓРµРј РїРµСЂСЃРѕРЅР°Р»СЊРЅСѓСЋ Р·Р°РјРµС‚РєСѓ (Р±РµР· С‡РёСЃРµР»), С‡С‚РѕР±С‹ С‚РµРєСЃС‚ Р±С‹Р» РјРµРЅРµРµ С€Р°Р±Р»РѕРЅРЅС‹Рј
            personal_line: str | None = None
            try:
                note = getattr(parsed, "rationale", None)
                intents = list(getattr(parsed, "intents", []) or [])
                if isinstance(note, str) and note.strip():
                    personal_line = _("РЈС‡С‘Р» Р·Р°РїСЂРѕСЃ: ") + note.strip()
                else:
                    intent_map = {
                        "low_fodmap_candidate": _("СѓРјРµРЅСЊС€РёС‚СЊ FODMAP-РїСЂРѕРґСѓРєС‚С‹"),
                        "lactose_free": _("РёР·Р±РµРіР°С‚СЊ Р»Р°РєС‚РѕР·С‹"),
                        "gluten_free": _("Р±РµР· РіР»СЋС‚РµРЅР°"),
                        "sugar_free": _("РѕРіСЂР°РЅРёС‡РёС‚СЊ СЃР°С…Р°СЂ"),
                        "keto": _("РєРµС‚Рѕ-СЃС…РµРјСѓ"),
                        "low_carb": _("СЃРЅРёР·РёС‚СЊ СѓРіР»РµРІРѕРґС‹"),
                        "high_protein": _("Р°РєС†РµРЅС‚ РЅР° Р±РµР»РѕРє"),
                        "raise_calories": _("СѓРІРµР»РёС‡РёС‚СЊ РєР°Р»РѕСЂРёР№РЅРѕСЃС‚СЊ"),
                        "lower_calories": _("СЃРЅРёР·РёС‚СЊ РєР°Р»РѕСЂРёР№РЅРѕСЃС‚СЊ"),
                        "activity_down": _("РїРѕРЅРёР·РёС‚СЊ Р°РєС‚РёРІРЅРѕСЃС‚СЊ"),
                        "activity_up": _("РїРѕРІС‹СЃРёС‚СЊ Р°РєС‚РёРІРЅРѕСЃС‚СЊ"),
                        "reduce_protein": _("СЃРЅРёР·РёС‚СЊ Р±РµР»РѕРє"),
                        "reduce_fat": _("СЃРЅРёР·РёС‚СЊ Р¶РёСЂС‹"),
                        "increase_fat": _("РїРѕРІС‹СЃРёС‚СЊ Р¶РёСЂС‹"),
                        "custom_macros": _("РєР°СЃС‚РѕРјРЅС‹Рµ РјР°РєСЂРѕСЃС‹"),
                    }
                    phrases = [intent_map[i] for i in intents if i in intent_map]
                    if phrases:
                        personal_line = _("РЈС‡С‘Р» Р·Р°РїСЂРѕСЃ: ") + ", ".join(phrases)
            except Exception:
                personal_line = None

            # 4.1) Р“РёР±СЂРёРґ (Р°СЃРёРЅС…СЂРѕРЅРЅРѕ): РїРµСЂРµС„СЂР°Р·РёСЂРѕРІР°С‚СЊ РѕР±СЉСЏСЃРЅРµРЅРёРµ РІ С„РѕРЅРµ Рё, РµСЃР»Рё СѓСЃРїРµРµС‚, РѕС‚СЂРµРґР°РєС‚РёСЂРѕРІР°С‚СЊ СЃРѕРѕР±С‰РµРЅРёРµ
            should_try_rephrase = (
                settings.ADJUST_REPHRASE_ENABLED
                and explanation
                and len(explanation) >= int(getattr(settings, "ADJUST_REPHRASE_LENGTH_MIN", 220) or 220)
            )

            # 5) РЎРѕС…СЂР°РЅРёРј
            adjustments = list(data_json.get("adjustments") or [])
            adjustments.append({
                "ts": getattr(message, "date", None).isoformat() if getattr(message, "date", None) else None,
                "text_raw": text,
                "parsed": {
                    "intents": getattr(parsed, "intents", None),
                    "activity_override": getattr(parsed, "activity_override", None),
                    "calories": getattr(parsed, "calories", None),
                    "macros": getattr(parsed, "macros", None),
                    "dietary_restrictions": getattr(parsed, "dietary_restrictions", None),
                    "confidence": getattr(parsed, "confidence", None),
                    "version": getattr(parsed, "version", None),
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
        await message.answer(_("РќРµ СѓРґР°Р»РѕСЃСЊ РїСЂРёРјРµРЅРёС‚СЊ РєРѕСЂСЂРµРєС‚РёСЂРѕРІРєСѓ. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р· РїРѕР·Р¶Рµ."))
        # Analytics: Adjust Fail (exception)
        try:
            if analytics.logger:
                analytics.fire_event(
                    BaseEvent(
                        user_id=getattr(message.from_user, "id", None),
                        event_type="Adjust:Fail",
                        event_properties=EventProperties(
                            chat_id=getattr(message.chat, "id", None),
                            chat_type=getattr(message.chat, "type", None),
                            text=None,
                            command=None,
                        ),
                        language=getattr(message.from_user, "language_code", None),
                        plan=Plan(branch="Adjust", source="onboarding", version="v1"),
                    )
                )
        except Exception:
            pass
        return

    # РџРѕРїСЂРѕР±СѓРµРј РїРѕРґРіРѕС‚РѕРІРёС‚СЊ РѕР±РЅРѕРІР»РµРЅРЅС‹Р№ РіСЂР°С„РёРє (Р±РµР· РЅРµРјРµРґР»РµРЅРЅРѕР№ РѕС‚РїСЂР°РІРєРё вЂ” РІР»РѕР¶РёРј РєР°Рє caption РЅРёР¶Рµ)
    png_data = None
    try:
        if settings.CHARTS_ENABLED:
            start_w = float(payload.weight_kg)
            goal_w = float(payload.goal_weight_kg) if payload.goal_weight_kg is not None else None
            weekly = float(getattr(new_plan, "weekly_rate_kg", 0.0) or 0.0)
            start_dt = getattr(message, "date", None)
            start_d = start_dt.date() if start_dt else date.today()
            eta = getattr(new_plan, "eta_date", None)
            logger.info("charts.try_send | phase=adjust | user_id={} | weekly={} | eta={}", user_id, weekly, eta)
            key_str = f"{start_w}:{goal_w}:{weekly}:{start_d.isoformat()}:{eta.isoformat() if eta else ''}:{settings.CHARTS_PRIVACY_MODE}:{settings.CHARTS_BAND_FRAC}:adjust"
            ph = hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]
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

    # 6) Р РµРЅРґРµСЂ РѕС‚РІРµС‚Р°
    lines: list[str] = []
    lines.append("<b>" + _("РўРІРѕР№ РїР»Р°РЅ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°РЅ!") + "</b>")
    lines.append("")
    if payload.goal != Goal.maintain:
        # ETA Рё СЃРєРѕСЂРѕСЃС‚СЊ
        if new_plan.eta_date is not None and payload.goal_weight_kg is not None:
            delta = abs(payload.weight_kg - payload.goal_weight_kg)
            formatted_date = new_plan.eta_date.strftime("%d.%m.%Y")
            if payload.goal == Goal.lose:
                lines.append(f"РўС‹ СЃР±СЂРѕСЃРёС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
            elif payload.goal == Goal.gain:
                lines.append(f"РўС‹ РЅР°Р±РµСЂРµС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
        lines.append(f"{_('РЎРєРѕСЂРѕСЃС‚СЊ')}: {new_plan.weekly_rate_kg} {_('РєРі РІ РЅРµРґРµР»СЋ')}")
    lines.append("")
    lines.append("<b>" + _("РћР±РЅРѕРІР»РµРЅРЅР°СЏ РґРЅРµРІРЅР°СЏ РЅРѕСЂРјР°:") + "</b>")
    lines.append(f"рџ”Ґ {_('РљР°Р»РѕСЂРёРё')}: {new_plan.calories} {_('РєРєР°Р»')}")
    lines.append(f"рџҐ© {_('Р‘РµР»РєРё')}: {new_plan.protein_g} {_('Рі')}")
    lines.append(f"рџҐ‘ {_('Р–РёСЂС‹')}: {new_plan.fat_g} {_('Рі')}")
    lines.append(f"рџЌћ {_('РЈРіР»РµРІРѕРґС‹')}: {new_plan.carbs_g} {_('Рі')}")
    lines.append("")
    if "personal_line" in locals() and personal_line:
        # Deduplicate: skip personal line if it repeats the explanation content
        def _norm_txt(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").lower()).strip()
        pl_core = re.sub(r"^СѓС‡[РµС‘]Р»\s+Р·Р°РїСЂРѕСЃ:\s*", "", personal_line, flags=re.IGNORECASE)
        if _norm_txt(pl_core) and _norm_txt(pl_core) not in _norm_txt(explanation):
            lines.append(personal_line)
    lines.append(explanation)
    lines.append("")
    lines.append(_("РћСЃС‚Р°РІРёРј С‚Р°Рє РёР»Рё РЅСѓР¶РЅР° РµС‰Рµ РєРѕСЂСЂРµРєС‚РёСЂРѕРІРєР°?"))

    kb = _ikb([
        [("Р’СЃС‘ РѕС‚Р»РёС‡РЅРѕ!", "final:ok")],
        [("РҐРѕС‡Сѓ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°С‚СЊ", "final:adjust")],
    ])

    # РЎРЅР°С‡Р°Р»Р° РїРѕРїСЂРѕР±СѓРµРј РѕС‚РїСЂР°РІРёС‚СЊ С„РѕС‚Рѕ СЃ РїРѕРґРїРёСЃСЊСЋ (РµРґРёРЅРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ)
    try:
        if png_data:
            caption = "\n".join(lines)
            if len(caption) <= 1024:
                await message.answer_photo(BufferedInputFile(png_data, filename="goal_plan.png"), caption=caption, reply_markup=kb)
                await state.set_state(OnboardingStates.review)
                return
            await message.answer_photo(BufferedInputFile(png_data, filename="goal_plan.png"), caption=lines[0])
            await message.answer(caption, reply_markup=kb, disable_web_page_preview=True)
            await state.set_state(OnboardingStates.review)
            return
    except Exception as e:
        logger.warning("charts.send_failed_caption | user_id={} | err={}", user_id, e)

    # Р¤РѕР»Р±СЌРє: РѕС‚РїСЂР°РІРёРј С‚РµРєСЃС‚РѕРј (РєР°Рє Р±С‹Р»Рѕ), С‡С‚РѕР±С‹ СЃРѕС…СЂР°РЅРёС‚СЊ rephrase-РїСѓС‚СЊ
    sent_msg = await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)

    # Р•СЃР»Рё РІРєР»СЋС‡РµРЅРѕ вЂ” Р·Р°РїСѓСЃС‚РёРј РїРµСЂРµС„СЂР°Р· РІ С„РѕРЅРµ Рё РїСЂРё СѓСЃРїРµС…Рµ РѕР±РЅРѕРІРёРј С‚РµРєСЃС‚ СЃРѕРѕР±С‰РµРЅРёСЏ
    if "should_try_rephrase" in locals() and should_try_rephrase:
        async def _rephrase_and_edit() -> None:
            try:
                rewritten = await rephrase_explanation_cached(explanation, settings.ADJUST_REPHRASE_TONE or "neutral")
                if not rewritten:
                    logger.info("adjust.rephrase.fallback | user_id={}", user_id)
                    return
                # РЎС„РѕСЂРјРёСЂРѕРІР°С‚СЊ РѕР±РЅРѕРІР»С‘РЅРЅС‹Р№ С‚РµРєСЃС‚ СЃ РїРµСЂРµС„СЂР°Р·РѕРј
                new_lines: list[str] = []
                new_lines.append("<b>" + _("РўРІРѕР№ РїР»Р°РЅ СЃРєРѕСЂСЂРµРєС‚РёСЂРѕРІР°РЅ!") + "</b>")
                new_lines.append("")
                if payload.goal != Goal.maintain:
                    if new_plan.eta_date is not None and payload.goal_weight_kg is not None:
                        delta = abs(payload.weight_kg - payload.goal_weight_kg)
                        formatted_date = new_plan.eta_date.strftime("%d.%m.%Y")
                        if payload.goal == Goal.lose:
                            new_lines.append(f"РўС‹ СЃР±СЂРѕСЃРёС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
                        elif payload.goal == Goal.gain:
                            new_lines.append(f"РўС‹ РЅР°Р±РµСЂРµС€СЊ {round(delta, 1)} РєРі Рє {formatted_date}")
                    new_lines.append(f"{_('РЎРєРѕСЂРѕСЃС‚СЊ')}: {new_plan.weekly_rate_kg} {_('РєРі РІ РЅРµРґРµР»СЋ')}")
                new_lines.append("")
                new_lines.append("<b>" + _("РћР±РЅРѕРІР»РµРЅРЅР°СЏ РґРЅРµРІРЅР°СЏ РЅРѕСЂРјР°:") + "</b>")
                new_lines.append(f"рџ”Ґ { _('РљР°Р»РѕСЂРёРё') }: {new_plan.calories} { _('РєРєР°Р»') }")
                new_lines.append(f"рџҐ© { _('Р‘РµР»РєРё') }: {new_plan.protein_g} { _('Рі') }")
                new_lines.append(f"рџҐ‘ { _('Р–РёСЂС‹') }: {new_plan.fat_g} { _('Рі') }")
                new_lines.append(f"рџЌћ { _('РЈРіР»РµРІРѕРґС‹') }: {new_plan.carbs_g} { _('Рі') }")
                new_lines.append("")
                if "personal_line" in locals() and personal_line:
                    def _norm_txt2(s: str) -> str:
                        return re.sub(r"\s+", " ", (s or "").lower()).strip()
                    pl_core2 = re.sub(r"^СѓС‡[РµС‘]Р»\s+Р·Р°РїСЂРѕСЃ:\s*", "", personal_line, flags=re.IGNORECASE)
                    if _norm_txt2(pl_core2) and _norm_txt2(pl_core2) not in _norm_txt2(rewritten):
                        new_lines.append(personal_line)
                new_lines.append(rewritten)
                new_lines.append("")
                new_lines.append(_("РћСЃС‚Р°РІРёРј С‚Р°Рє РёР»Рё РЅСѓР¶РЅР° РµС‰Рµ РєРѕСЂСЂРµРєС‚РёСЂРѕРІРєР°?"))
                try:
                    await sent_msg.edit_text("\n".join(new_lines), reply_markup=kb)
                    logger.info("adjust.rephrase.applied | user_id={} | len={}", user_id, len(rewritten))
                except Exception as e:
                    logger.warning("adjust.rephrase.edit_failed | user_id={} | err={}", user_id, e)
            except Exception as e:
                logger.warning("adjust.rephrase.error | user_id={} | err={}", user_id, e)

        asyncio.create_task(_rephrase_and_edit())

    await state.set_state(OnboardingStates.review)


