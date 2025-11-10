from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta, timezone, date, time as dtime
from typing import Any, Optional

from loguru import logger
from collections import deque
from sqlalchemy import select, update

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import (
    DailyIntakeModel,
    OnboardingAnswerModel,
    DailyReportLogModel,
    UserModel,
)
from bot.metrics import (
    daily_report_started,
    daily_report_sent,
    daily_report_failed,
    daily_report_fallback,
    daily_report_duration_ms,
)
from bot.services.users import get_user_tzinfo
from bot.services.foodai import _openai_request  # type: ignore
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError


_MOTIVATION_SHORT_POOL: list[str] = [
    "✨Супер! Первый день позади. Ещё вчера это казалось сложным, а сегодня уже получается!",
    "✨Отлично! Еще один день в плюс — ты на шаг ближе к фигуре мечты!",
    "✨Круто! Ты не сдаёшься — результат уже не за горами!",
    "✨Ты молодец! Маленькие шаги складываются в большой результат.",
    "✨Продолжай в том же духе — стабильность сильнее мотивации!",
    "✨Каждый осознанный выбор делает тебя здоровее.",
]

_rps_events = deque(maxlen=200)

async def _limit_telegram_rps() -> None:
    """Global RPS limiter for Telegram sends.
    Ensures no more than DAILY_REPORTS_TELEGRAM_RPS messages per second.
    """
    try:
        limit = int(getattr(settings, "DAILY_REPORTS_TELEGRAM_RPS", 10) or 10)
    except Exception:
        limit = 10
    if limit <= 0:
        return
    while True:
        now = time.time()
        # drop events older than 1s
        while _rps_events and (now - _rps_events[0]) > 1.0:
            _rps_events.popleft()
        if len(_rps_events) < limit:
            _rps_events.append(now)
            return
        await asyncio.sleep(0.05)


def _yesterday_local_window(tz) -> tuple[datetime, datetime, date]:
    now_local = datetime.now(tz)
    y_local = (now_local - timedelta(days=1)).date()
    start = datetime.combine(y_local, dtime(0, 0), tz)
    end = start + timedelta(days=1)
    return start, end, y_local


async def _fetch_plan_and_fact(user_id: int) -> tuple[dict[str, float], dict[str, float], date]:
    plan = {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    fact = {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)
        start_local, end_local, y_local = _yesterday_local_window(tz)
        # Overlapping UTC dates that cover local 24h window
        d1 = start_local.astimezone(timezone.utc).date()
        d2 = (end_local - timedelta(seconds=1)).astimezone(timezone.utc).date()
        date_candidates = {d1, d2}

        oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        dp = (oa.daily_plan if oa and isinstance(getattr(oa, "daily_plan", None), dict) else {}) or {}
        try:
            plan = {
                "calories": float(dp.get("calories") or 0),
                "protein_g": float(dp.get("protein_g") or 0),
                "fat_g": float(dp.get("fat_g") or 0),
                "carbs_g": float(dp.get("carbs_g") or 0),
            }
        except Exception:
            pass

        res = await session.execute(
            select(DailyIntakeModel).where(
                (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc.in_(date_candidates))
            )
        )
        rows = list(res.scalars().all())
        if rows:
            try:
                c = sum(float(r.calories or 0) for r in rows)
                p = sum(float(r.protein_g or 0) for r in rows)
                f = sum(float(r.fat_g or 0) for r in rows)
                cb = sum(float(r.carbs_g or 0) for r in rows)
                fact = {"calories": c, "protein_g": p, "fat_g": f, "carbs_g": cb}
            except Exception:
                pass
    return plan, fact, y_local


def _pct(fact: float, plan: float) -> int:
    if plan > 0:
        return int(round((fact / plan) * 100.0))
    return 0


def _fmt_summary_text(date_local: date, plan: dict[str, float], fact: dict[str, float], short: str, long: str, advice: str) -> str:
    d = date_local.strftime("%d.%m.%Y")
    cal_f, cal_p = int(fact.get("calories", 0)), int(plan.get("calories", 0))
    p_f, p_p = float(fact.get("protein_g", 0.0)), float(plan.get("protein_g", 0.0))
    f_f, f_p = float(fact.get("fat_g", 0.0)), float(plan.get("fat_g", 0.0))
    c_f, c_p = float(fact.get("carbs_g", 0.0)), float(plan.get("carbs_g", 0.0))

    lines = [
        f"📊 Отчёт за {d}",
        "",
        short.strip(),
        "",
        f"🔥 Калории: {cal_f} / {cal_p} ({_pct(cal_f, cal_p)}%)",
        f"🥩 Белки: {p_f:.1f} / {p_p:.1f} ({_pct(p_f, p_p)}%)",
        f"🥑 Жиры: {f_f:.1f} / {f_p:.1f} ({_pct(f_f, f_p)}%)",
        f"🍞 Углеводы: {c_f:.1f} / {c_p:.1f} ({_pct(c_f, c_p)}%)",
        "",
        long.strip(),
        "",
        advice.strip(),
    ]
    return "\n".join(lines)


def _pick_short_motivation() -> str:
    return random.choice(_MOTIVATION_SHORT_POOL)


async def _gen_llm_content(plan: dict[str, float], fact: dict[str, float]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    model = (settings.DAILY_REPORTS_MODEL or settings.RECOMMENDER_MODEL or settings.FOODAI_DEFAULT_MODEL or "gpt-5-mini")
    timeout = int(getattr(settings, "DAILY_REPORTS_LLM_TIMEOUT_SEC", 10) or 10)

    system = (
        "Ты — ИИ-нутрициолог. Сформируй две части на РУССКОМ: 'motivation_full' (200-400 символов) и 'advice' (400-800). "
        "Формат ответа СТРОГО JSON {\"motivation_full\": str, \"advice\": str}. "
        "Избегай медицинских диагнозов и опасных рекомендаций. Тон — поддерживающий и конкретный."
    )
    user_text = (
        "План на день: калории {pc}, белки {pp} г, жиры {pf} г, углеводы {pcb} г. "
        "Факт за вчера: калории {fc}, белки {fp} г, жиры {ff} г, углеводы {fcb} г. "
        "Дай мотивацию и практичные советы, без списков покупок."
    ).format(
        pc=int(plan.get("calories", 0)), pp=round(plan.get("protein_g", 0.0), 1), pf=round(plan.get("fat_g", 0.0), 1), pcb=round(plan.get("carbs_g", 0.0), 1),
        fc=int(fact.get("calories", 0)), fp=round(fact.get("protein_g", 0.0), 1), ff=round(fact.get("fat_g", 0.0), 1), fcb=round(fact.get("carbs_g", 0.0), 1),
    )

    payload: dict[str, Any] = {
        "model": model,
        "instructions": system,
        "text": {"verbosity": getattr(settings, "FOODAI_TEXT_VERBOSITY", "low")},
        "max_output_tokens": 600,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                ],
            }
        ],
    }

    raw: Optional[str] = None
    try:
        raw = await asyncio.wait_for(_openai_request("responses", payload), timeout=timeout)
        if not raw:
            return None, None, "empty"
        data: Optional[dict[str, Any]] = None
        try:
            data = json.loads(raw)
        except Exception:
            t = (raw or "").strip()
            s = t.find("{")
            e = t.rfind("}")
            if s != -1 and e != -1 and e > s:
                try:
                    data = json.loads(t[s:e+1])
                except Exception:
                    data = None
        if not isinstance(data, dict):
            return None, None, "json_parse"
        mot = str(data.get("motivation_full") or "").strip()
        adv = str(data.get("advice") or "").strip()
        if not mot or not adv:
            return None, None, "invalid"
        return mot, adv, None
    except asyncio.TimeoutError:
        return None, None, "timeout"
    except Exception as e:
        try:
            logger.warning("daily_report_llm_error | err={}", e)
        except Exception:
            pass
        return None, None, "other"


async def assemble_and_send_report(bot: Bot, user_id: int, *, scheduled_epoch: Optional[int] = None) -> bool:
    t0 = time.time()
    try:
        daily_report_started.inc()
    except Exception:
        pass

    plan, fact, y_local = await _fetch_plan_and_fact(user_id)

    # Subscription gating: skip if premium required and user is not premium
    if getattr(settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False):
        async with sessionmaker() as session:
            is_premium = bool(
                await session.scalar(
                    select(UserModel.is_premium).where(UserModel.id == user_id)
                )
                or False
            )
            if not is_premium:
                existing = await session.scalar(
                    select(DailyReportLogModel).where(
                        (DailyReportLogModel.user_id == user_id) & (DailyReportLogModel.date_local == y_local)
                    )
                )
                if existing is None:
                    try:
                        session.add(
                            DailyReportLogModel(
                                user_id=user_id,
                                date_local=y_local,
                                status="skipped",
                                error_code="not_subscribed",
                                error_text=None,
                            )
                        )
                        await session.commit()
                    except Exception:
                        await session.rollback()
                else:
                    if existing.status != "sent":
                        await session.execute(
                            update(DailyReportLogModel)
                            .where(
                                (DailyReportLogModel.user_id == user_id)
                                & (DailyReportLogModel.date_local == y_local)
                            )
                            .values(status="skipped", error_code="not_subscribed")
                        )
                        await session.commit()
                return False

    # Idempotency: ensure single send per (user_id, date_local)
    async with sessionmaker() as session:
        existing = await session.scalar(
            select(DailyReportLogModel).where(
                (DailyReportLogModel.user_id == user_id) & (DailyReportLogModel.date_local == y_local)
            )
        )
        if existing and existing.status == "sent":
            return
        if not existing:
            try:
                session.add(DailyReportLogModel(user_id=user_id, date_local=y_local, status="queued"))
                await session.commit()
            except Exception:
                await session.rollback()

    short = _pick_short_motivation()

    mot, adv, err = await _gen_llm_content(plan, fact)
    used_fallback = False
    if err is not None and getattr(settings, "DAILY_REPORTS_FALLBACK_ENABLED", True):
        used_fallback = True
        # Simple heuristic fallback based on deficits/excess
        msgs: list[str] = []
        if plan.get("protein_g", 0) > 0 and fact.get("protein_g", 0) < plan.get("protein_g", 0) * 0.85:
            msgs.append("Добавь завтра белковый завтрак: яйца, творог, йогурт или курицу.")
        if fact.get("calories", 0) > plan.get("calories", 0) * 1.05:
            msgs.append("Сократи быстрые углеводы и сладкие напитки — они легко разгоняют калории.")
        if not msgs:
            msgs.append("Запланируй полноценный завтрак и держи воду под рукой. Ты справишься!")
        mot = "Двигаешься в верном направлении. Держим курс — по шагу каждый день!"
        adv = "\n".join(f"• {m}" for m in msgs)

    text = _fmt_summary_text(y_local, plan, fact, short, mot or "", adv or "")

    # Send and update log
    msg_id: Optional[int] = None
    try:
        await _limit_telegram_rps()
        sent = await bot.send_message(chat_id=user_id, text=text)
        msg_id = sent.message_id if sent else None
        try:
            daily_report_sent.inc()
            if used_fallback:
                daily_report_fallback.inc()
        except Exception:
            pass
        async with sessionmaker() as session:
            await session.execute(
                update(DailyReportLogModel)
                .where((DailyReportLogModel.user_id == user_id) & (DailyReportLogModel.date_local == y_local))
                .values(status="sent", message_id=msg_id, sent_at_utc=datetime.now(timezone.utc))
            )
            await session.commit()
        return True
    except TelegramForbiddenError as e:
        # User blocked the bot — mark skipped and do not reschedule
        try:
            daily_report_failed.labels("telegram").inc()
        except Exception:
            pass
        async with sessionmaker() as session:
            await session.execute(
                update(DailyReportLogModel)
                .where((DailyReportLogModel.user_id == user_id) & (DailyReportLogModel.date_local == y_local))
                .values(status="skipped", error_code="telegram_forbidden", error_text=str(e))
            )
            await session.commit()
        return False
    except Exception as e:
        try:
            daily_report_failed.labels("telegram").inc()
        except Exception:
            pass
        async with sessionmaker() as session:
            await session.execute(
                update(DailyReportLogModel)
                .where((DailyReportLogModel.user_id == user_id) & (DailyReportLogModel.date_local == y_local))
                .values(status="failed", error_code="telegram", error_text=str(e))
            )
            await session.commit()
        return True
    finally:
        try:
            daily_report_duration_ms.observe((time.time() - t0) * 1000)
        except Exception:
            pass
