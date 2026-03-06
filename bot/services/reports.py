from __future__ import annotations
import asyncio
import contextlib
import json
import random
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from sqlalchemy import select, update

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import (
    DailyIntakeModel,
    DailyReportLogModel,
    OnboardingAnswerModel,
    WeightLogModel,
)
from bot.metrics import (
    daily_report_duration_ms,
    daily_report_failed,
    daily_report_fallback,
    daily_report_sent,
    daily_report_started,
)
from bot.services.foodai import _openai_request  # type: ignore
from bot.services.users import get_user_tzinfo, is_subscription_active

if TYPE_CHECKING:
    from aiogram import Bot

_MOTIVATION_SHORT_POOL: list[str] = [
    "✨Супер! Первый день позади. Ещё вчера это казалось сложным, а сегодня уже получается!",
    "✨Отлично! Еще один день в плюс — ты на шаг ближе к фигуре мечты!",
    "✨Круто! Ты не сдаёшься — результат уже не за горами!",
    "✨Ты молодец! Маленькие шаги складываются в большой результат.",
    "✨Продолжай в том же духе — стабильность сильнее мотивации!",
    "✨Каждый осознанный выбор делает тебя здоровее.",
    "✨Замечательно! Каждый день — новая победа над собой!",
    "✨Ты на правильном пути: сегодняшние усилия — завтрашний результат!",
    "✨Отлично сработано! Даже маленький прогресс — это движение вперёд.",
    "✨Горжусь тобой! Ты доказываешь, что упорство творит чудеса.",
    "✨Каждый твой шаг приближает тебя к цели — так держать!",
    "✨Ты делаешь это! День за днём ты становишься лучше.",
    "✨Прекрасно! Ты формируешь привычки, которые изменят твою жизнь.",
    "✨Не останавливайся — твои усилия уже дают плоды!",
    "✨Ты в игре! Каждый день добавляет очков в копилку успеха.",
    "✨Молодец! Ты выбираешь здоровье и силу каждый день.",
    "✨Сегодня ты снова доказал: постоянство — ключ к результату!",
    "✨Потрясающе! Ты создаёшь будущее своими сегодняшними действиями.",
    "✨Продолжай — твоя дисциплина уже работает на тебя!",
    "✨Ты круче, чем думаешь: каждый день ты растёшь над собой.",
    "✨Отлично! Ты пишешь историю своего успеха — по одной странице в день.",
    "✨Ты в потоке: ежедневные усилия превращаются в большие достижения.",
    "✨Восхищаюсь твоей настойчивостью! Ты точно добьёшься цели.",
    "✨Каждый день — новый шанс стать лучше. И ты им пользуешься!",
    "✨Ты двигаешься вперёд, и это самое главное. Продолжай!",
    "✨Сегодняшний день — ещё один кирпичик в фундаменте твоего успеха!",
]

_rps_events = deque(maxlen=200)


def _len_limits() -> tuple[int, int, int, int]:
    try:
        mot_min = int(getattr(settings, "DAILY_REPORTS_TARGET_LEN_MOTIVATION_MIN", 300) or 300)
    except Exception:
        mot_min = 300
    try:
        mot_max = int(getattr(settings, "DAILY_REPORTS_TARGET_LEN_MOTIVATION_MAX", 350) or 350)
    except Exception:
        mot_max = 350
    try:
        adv_min = int(getattr(settings, "DAILY_REPORTS_TARGET_LEN_ADVICE_MIN", 200) or 200)
    except Exception:
        adv_min = 200
    try:
        adv_max = int(getattr(settings, "DAILY_REPORTS_TARGET_LEN_ADVICE_MAX", 300) or 300)
    except Exception:
        adv_max = 300
    return mot_min, mot_max, adv_min, adv_max


def _in_range(txt: str, lo: int, hi: int) -> bool:
    l = len((txt or "").strip())
    return l >= lo and l <= hi


def _compose_neutral_text(pool: list[str], lo: int, hi: int) -> str:
    def join_len(parts: list[str]) -> int:
        if not parts:
            return 0
        return sum(len(p) for p in parts) + (len(parts) - 1)

    sents = [s.strip() for s in (pool or []) if isinstance(s, str) and s.strip()]
    # 1) exact single sentence in range
    for s in sents:
        L = len(s)
        if lo <= L <= hi:
            return s
    # 2) try pairs
    best: list[str] | None = None
    best_len = -1
    n = len(sents)
    for i in range(n):
        for j in range(i + 1, n):
            parts = [sents[i], sents[j]]
            L = join_len(parts)
            if lo <= L <= hi:
                return " ".join(parts)
            if hi >= L and best_len < L:
                best, best_len = parts, L
    # 3) try triples
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                parts = [sents[i], sents[j], sents[k]]
                L = join_len(parts)
                if lo <= L <= hi:
                    return " ".join(parts)
                if hi >= L and best_len < L:
                    best, best_len = parts, L
    # 4) fallback to the longest <= hi if exists, otherwise the shortest sentence
    if best is not None and best_len >= 0 and best_len >= lo:
        return " ".join(best)
    # longest single <= hi
    single_best = ""
    for s in sents:
        L = len(s)
        if hi >= L and len(single_best) < L:
            single_best = s
    if single_best:
        return single_best
    # last resort: return the shortest (still no truncation)
    return min(sents, key=len) if sents else ""


def _neutral_motivation() -> str:
    mot_min, mot_max, _, _ = _len_limits()
    sentences = [
        "Сегодня важен не идеальный результат, а стабильность. Действуй в спокойном темпе и опирайся на простые шаги, которые реально выполнимы именно для тебя в текущем дне.",
        "Небольшие шаги формируют привычку и дают устойчивый прогресс без перегибов. Поддерживай внимание к питанию и самочувствию, а остальное придёт естественно со временем.",
        "Сделай акцент на ясной цели на день и будь добрее к себе: так легче сохранять курс и возвращаться в режим, если что-то пошло не по плану."
    ]
    return _compose_neutral_text(sentences, mot_min, mot_max)


def _neutral_advice() -> str:
    _, _, adv_min, adv_max = _len_limits()
    sentences = [
        "Держи под рукой воду и распредели приёмы пищи равномерно в течение дня, чтобы избежать больших провалов в энергии.",
        "Собери тарелку из простых продуктов: источник белка, овощи и умеренная порция сложных углеводов — этого достаточно, чтобы чувствовать контроль.",
        "План на вечер сделай лёгким и заканчивай приём пищи за пару часов до сна — так и сон, и утро будут стабильнее."
    ]
    return _compose_neutral_text(sentences, adv_min, adv_max)

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
        with contextlib.suppress(Exception):
            plan = {
                "calories": float(dp.get("calories") or 0),
                "protein_g": float(dp.get("protein_g") or 0),
                "fat_g": float(dp.get("fat_g") or 0),
                "carbs_g": float(dp.get("carbs_g") or 0),
            }

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
        return round((fact / plan) * 100.0)
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
        (f"👌🏼 {long.strip()}" if (long or "").strip() else ""),
        "",
        (f"✅ {advice.strip()}" if (advice or "").strip() else ""),
    ]
    return "\n".join(lines)


def _pick_short_motivation() -> str:
    return random.choice(_MOTIVATION_SHORT_POOL)


async def _collect_user_context(user_id: int) -> dict[str, Any]:
    """Collect extended context for LLM: weights, goals, progress, trend, adherence streak.
    Returns a dict with keys: current_weight, goal_weight, start_weight, progress_pct,
    trend_7d, adherence_streak_days, logging_days_last7.
    """
    ctx: dict[str, Any] = {
        "current_weight": None,
        "goal_weight": None,
        "start_weight": None,
        "progress_pct": None,
        "trend_7d": None,
        "adherence_streak_days": 0,
        "logging_days_last7": 0,
    }
    async with sessionmaker() as session:
        tz = await get_user_tzinfo(session, user_id)

        # Weights: current, goal, start
        latest = await session.execute(
            select(WeightLogModel)
            .where(WeightLogModel.user_id == user_id)
            .order_by(WeightLogModel.recorded_at.desc())
            .limit(1)
        )
        last_row = latest.scalars().first()
        if last_row and last_row.weight_kg is not None:
            with contextlib.suppress(Exception):
                ctx["current_weight"] = float(last_row.weight_kg)

        oa = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        oa_data = (oa.data if oa and isinstance(getattr(oa, "data", None), dict) else {}) or {}
        if oa_data.get("goal_weight_kg") is not None:
            with contextlib.suppress(Exception):
                ctx["goal_weight"] = float(oa_data.get("goal_weight_kg"))

        first = await session.execute(
            select(WeightLogModel)
            .where(WeightLogModel.user_id == user_id)
            .order_by(WeightLogModel.recorded_at.asc())
            .limit(1)
        )
        first_row = first.scalars().first()
        if first_row and first_row.weight_kg is not None:
            with contextlib.suppress(Exception):
                ctx["start_weight"] = float(first_row.weight_kg)
        if ctx["start_weight"] is None:
            # fallback to onboarding start/current
            if oa_data.get("start_weight_kg") is not None:
                with contextlib.suppress(Exception):
                    ctx["start_weight"] = float(oa_data.get("start_weight_kg"))
            elif oa_data.get("weight_kg") is not None:
                with contextlib.suppress(Exception):
                    ctx["start_weight"] = float(oa_data.get("weight_kg"))

        # Progress pct towards goal
        sw = ctx.get("start_weight")
        cw = ctx.get("current_weight")
        gw = ctx.get("goal_weight")
        try:
            if sw is not None and cw is not None and gw is not None and gw != sw:
                if gw < sw:
                    numerator = max(0.0, sw - cw)
                    denom = max(0.0, sw - gw)
                else:
                    numerator = max(0.0, cw - sw)
                    denom = max(0.0, gw - sw)
                ctx["progress_pct"] = 0.0 if denom <= 0 else max(0.0, min(100.0, (numerator / denom) * 100.0))
        except Exception:
            ctx["progress_pct"] = None

        # Trend over ~7 days (local)
        rows_all = await session.execute(
            select(WeightLogModel)
            .where(WeightLogModel.user_id == user_id)
            .order_by(WeightLogModel.recorded_at.asc())
        )
        wrows = list(rows_all.scalars().all())
        if wrows:
            try:
                start7 = (datetime.now(tz) - timedelta(days=7)).date()
                base = None
                for r in wrows:
                    if r.recorded_local_date >= start7:
                        base = r
                        break
                base = base or wrows[0]
                last = wrows[-1]
                if base and last and base.weight_kg is not None and last.weight_kg is not None:
                    ctx["trend_7d"] = float(last.weight_kg) - float(base.weight_kg)
            except Exception:
                pass

        # Adherence: streak of consecutive days with any intake ending yesterday; and logging_days_last7
        streak = 0
        broken = False
        logged7 = 0
        for i in range(1, 8):
            day = (datetime.now(tz) - timedelta(days=i)).date()
            start_local = datetime.combine(day, dtime(0, 0), tz)
            end_local = start_local + timedelta(days=1)
            d1 = start_local.astimezone(timezone.utc).date()
            d2 = (end_local - timedelta(seconds=1)).astimezone(timezone.utc).date()
            resi = await session.execute(
                select(DailyIntakeModel)
                .where((DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc.in_({d1, d2})))
                .limit(1)
            )
            has = resi.scalars().first() is not None
            if has:
                logged7 += 1
                if not broken:
                    streak += 1
            else:
                broken = True
        ctx["adherence_streak_days"] = streak
        ctx["logging_days_last7"] = logged7

    return ctx


async def _gen_llm_content(plan: dict[str, float], fact: dict[str, float], ctx: dict[str, Any] | None = None) -> tuple[str | None, str | None, str | None]:
    model = (settings.DAILY_REPORTS_MODEL or settings.RECOMMENDER_MODEL or settings.FOODAI_DEFAULT_MODEL or "gpt-5-mini")
    timeout = int(getattr(settings, "DAILY_REPORTS_LLM_TIMEOUT_SEC", 10) or 10)
    mot_min, mot_max, adv_min, adv_max = _len_limits()

    system = (
        "Ты — ИИ-нутрициолог и персональный коуч. Верни СТРОГО JSON с полями 'motivation_full' и 'advice' без преамбул. "
        f"Требования к длине: motivation_full {mot_min}-{mot_max} символов; advice {adv_min}-{adv_max} символов. "
        "Каждое поле — один абзац, без списков и эмодзи. Язык — русский. "
        "Учитывай план/факт по КБЖУ, прогресс по весу, тренд 7 дней и дисциплину (streak). "
        "Избегай медицинских диагнозов/лекарств и опасных рекомендаций. Тон поддерживающий и реалистичный. "
        "Заверши каждое поле полной фразой на точке. Не обрывай слова и не используй переносы слов/дефисы для переноса (никаких дефисов на конце строк). "
        "Ориентируйся на продукты и формулировки, привычные в России: общие названия (гречка, овсянка, творог, кефир/ряженка, куриная грудка, яйца, рыба, цельнозерновой хлеб, овощи, фрукты по сезону), без зарубежных брендов. "
        "Единицы измерения — граммы, миллилитры и порции; не используй cups/ounces и англоязычные сокращения."
    )

    # Build user context string
    cw = ctx.get("current_weight") if ctx else None
    gw = ctx.get("goal_weight") if ctx else None
    sw = ctx.get("start_weight") if ctx else None
    prog = ctx.get("progress_pct") if ctx else None
    tr7 = ctx.get("trend_7d") if ctx else None
    streak = ctx.get("adherence_streak_days") if ctx else None
    logged7 = ctx.get("logging_days_last7") if ctx else None

    def _fmt(v: float | None, suf: str = "") -> str:
        return (f"{v:.1f}{suf}" if isinstance(v, (int, float)) else "нет данных")

    plan_line = "План на день: калории {pc}, белки {pp} г, жиры {pf} г, углеводы {pcb} г.".format(
        pc=int(plan.get("calories", 0)), pp=round(plan.get("protein_g", 0.0), 1), pf=round(plan.get("fat_g", 0.0), 1), pcb=round(plan.get("carbs_g", 0.0), 1)
    )
    fact_line = "Факт за вчера: калории {fc}, белки {fp} г, жиры {ff} г, углеводы {fcb} г.".format(
        fc=int(fact.get("calories", 0)), fp=round(fact.get("protein_g", 0.0), 1), ff=round(fact.get("fat_g", 0.0), 1), fcb=round(fact.get("carbs_g", 0.0), 1)
    )
    ctx_line = (
        "Контекст: текущий вес {cw} кг; целевой {gw} кг; старт {sw} кг; прогресс к цели {pr}%; тренд_7д {tr}; "
        "стрик дисциплины (дней подряд с едой до вчера) {st}; активность логирования за 7 дней {lg}/7."
    ).format(
        cw=_fmt(cw), gw=_fmt(gw), sw=_fmt(sw), pr=(f"{prog:.0f}" if isinstance(prog, (int, float)) else "нет данных"),
        tr=(f"{tr7:+.1f} кг" if isinstance(tr7, (int, float)) else "нет данных"), st=(streak or 0), lg=(logged7 or 0)
    )

    user_text = f"{plan_line} {fact_line} {ctx_line} Сформируй персональную мотивацию и практичные советы на 1 день: питание, режим, поведенческие рекомендации. Без списков покупок."

    payload: dict[str, Any] = {
        "model": model,
        "instructions": system,
        "reasoning": {"effort": getattr(settings, "FOODAI_REASONING_EFFORT", "minimal")},
        "text": {
            "verbosity": getattr(settings, "FOODAI_TEXT_VERBOSITY", "low"),
            "format": {
                "type": "json_schema",
                "name": "daily_report",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "motivation_full": {"type": "string", "minLength": mot_min, "maxLength": mot_max},
                        "advice": {"type": "string", "minLength": adv_min, "maxLength": adv_max}
                    },
                    "required": ["motivation_full", "advice"],
                    "additionalProperties": False
                }
            }
        },
        "max_output_tokens": 800,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                ],
            }
        ],
    }
    async def _call_and_parse(pl: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        raw: str | None = None
        try:
            raw = await asyncio.wait_for(_openai_request("responses", pl), timeout=timeout)
            if not raw:
                with contextlib.suppress(Exception):
                    logger.warning("daily_report_llm_empty | sample={}", (raw or "")[:200])
                return None, None, "empty"
            data: dict[str, Any] | None = None
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
                with contextlib.suppress(Exception):
                    logger.warning("daily_report_llm_bad_json | sample={}", (raw or "")[:200])
                return None, None, "json_parse"
            mot = str(data.get("motivation_full") or "").strip()
            adv = str(data.get("advice") or "").strip()
            if not mot or not adv:
                with contextlib.suppress(Exception):
                    logger.warning("daily_report_llm_invalid_fields | sample={}", (raw or "")[:200])
                return None, None, "invalid"
            return mot, adv, None
        except asyncio.TimeoutError:
            return None, None, "timeout"
        except Exception as e:
            with contextlib.suppress(Exception):
                logger.warning("daily_report_llm_error | err={}", e)
            return None, None, "other"

    attempts = 0
    max_attempts = int(getattr(settings, "DAILY_REPORTS_LLM_MAX_ATTEMPTS", 2) or 2)
    mot_out: str | None = None
    adv_out: str | None = None
    last_err: str | None = None
    while attempts < max_attempts:
        attempts += 1
        mot_out, adv_out, err = await _call_and_parse(payload)
        if err is None and _in_range(mot_out or "", mot_min, mot_max) and _in_range(adv_out or "", adv_min, adv_max):
            return mot_out, adv_out, None
        # One refine attempt if lengths off and we have some text
        if err is None and (mot_out or adv_out):
            refine_instructions = (
                "Сохрани смысл и переформулируй текст строго в заданные диапазоны символов. "
                f"motivation_full {mot_min}-{mot_max}; advice {adv_min}-{adv_max}. "
                "Заверши оба поля полной фразой на точке. Не обрывай слова и не используй переносы слов/дефисы для переноса. "
                "Сохрани российский контекст продуктов и единицы измерения (г, мл, порции); не добавляй бренды и англоязычные единицы. "
                "Верни только JSON."
            )
            refined_payload = {
                "model": model,
                "instructions": system,
                "reasoning": {"effort": getattr(settings, "FOODAI_REASONING_EFFORT", "minimal")},
                "text": {
                    "verbosity": getattr(settings, "FOODAI_TEXT_VERBOSITY", "low"),
                    "format": {
                        "type": "json_schema",
                        "name": "daily_report",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "motivation_full": {"type": "string", "minLength": mot_min, "maxLength": mot_max},
                                "advice": {"type": "string", "minLength": adv_min, "maxLength": adv_max}
                            },
                            "required": ["motivation_full", "advice"],
                            "additionalProperties": False
                        }
                    }
                },
                "max_output_tokens": 800,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": user_text},
                            {"type": "input_text", "text": "Текущий JSON:"},
                            {"type": "input_text", "text": json.dumps({"motivation_full": mot_out, "advice": adv_out}, ensure_ascii=False)},
                            {"type": "input_text", "text": refine_instructions},
                        ],
                    }
                ],
            }
            mot_r, adv_r, err_r = await _call_and_parse(refined_payload)
            if err_r is None and _in_range(mot_r or "", mot_min, mot_max) and _in_range(adv_r or "", adv_min, adv_max):
                return mot_r, adv_r, None
            last_err = err_r or "range"
        else:
            last_err = err or "other"
        with contextlib.suppress(Exception):
            await asyncio.sleep(0.3 * (2 ** (attempts - 1)))
    return None, None, (last_err or "failed")


async def assemble_and_send_report(bot: Bot, user_id: int, *, scheduled_epoch: int | None = None) -> bool:
    t0 = time.time()
    with contextlib.suppress(Exception):
        daily_report_started.inc()

    plan, fact, y_local = await _fetch_plan_and_fact(user_id)

    # Subscription gating: skip if premium required and user has no active subscription
    if getattr(settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False):
        async with sessionmaker() as session:
            active = await is_subscription_active(session, user_id)
            if not active:
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
                elif existing.status != "sent":
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

    try:
        days_req = int(getattr(settings, "DAILY_REPORTS_REQUIRE_ACTIVITY_DAYS", 0) or 0)
    except Exception:
        days_req = 0
    if days_req > 0:
        active = False
        async with sessionmaker() as session:
            tz = await get_user_tzinfo(session, user_id)
            utc_dates: set[date] = set()
            now_local = datetime.now(tz)
            for i in range(1, days_req + 1):
                dloc = (now_local - timedelta(days=i)).date()
                s = datetime.combine(dloc, dtime(0, 0), tz)
                e = s + timedelta(days=1)
                d1 = s.astimezone(timezone.utc).date()
                d2 = (e - timedelta(seconds=1)).astimezone(timezone.utc).date()
                utc_dates.update({d1, d2})
            resi = await session.execute(
                select(DailyIntakeModel).where(
                    (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc.in_(utc_dates))
                ).limit(1)
            )
            active = resi.scalars().first() is not None
            if not active:
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
                                error_code="inactive",
                                error_text=None,
                            )
                        )
                        await session.commit()
                    except Exception:
                        await session.rollback()
                elif existing.status != "sent":
                    await session.execute(
                        update(DailyReportLogModel)
                        .where(
                            (DailyReportLogModel.user_id == user_id)
                            & (DailyReportLogModel.date_local == y_local)
                        )
                        .values(status="skipped", error_code="inactive")
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
            return None
        if not existing:
            try:
                session.add(DailyReportLogModel(user_id=user_id, date_local=y_local, status="queued"))
                await session.commit()
            except Exception:
                await session.rollback()

    short = _pick_short_motivation()

    # Collect extended context for personalized coaching
    ctx = await _collect_user_context(user_id)
    mot, adv, err = await _gen_llm_content(plan, fact, ctx)
    used_fallback = False
    if err is not None and getattr(settings, "DAILY_REPORTS_FALLBACK_ENABLED", True):
        used_fallback = True
        mot = _neutral_motivation()
        adv = _neutral_advice()

    text = _fmt_summary_text(y_local, plan, fact, short, mot or "", adv or "")

    # Send and update log
    msg_id: int | None = None
    try:
        await _limit_telegram_rps()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⚖️ Мой вес", callback_data="weight:open:report"),
                    InlineKeyboardButton(text="💻 Личный кабинет", callback_data="account:open:today"),
                ]
            ]
        )
        sent = await bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
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
        with contextlib.suppress(Exception):
            daily_report_failed.labels("telegram").inc()
        async with sessionmaker() as session:
            await session.execute(
                update(DailyReportLogModel)
                .where((DailyReportLogModel.user_id == user_id) & (DailyReportLogModel.date_local == y_local))
                .values(status="skipped", error_code="telegram_forbidden", error_text=str(e))
            )
            await session.commit()
        return False
    except Exception as e:
        with contextlib.suppress(Exception):
            daily_report_failed.labels("telegram").inc()
        async with sessionmaker() as session:
            await session.execute(
                update(DailyReportLogModel)
                .where((DailyReportLogModel.user_id == user_id) & (DailyReportLogModel.date_local == y_local))
                .values(status="failed", error_code="telegram", error_text=str(e))
            )
            await session.commit()
        return True
    finally:
        with contextlib.suppress(Exception):
            daily_report_duration_ms.observe((time.time() - t0) * 1000)
