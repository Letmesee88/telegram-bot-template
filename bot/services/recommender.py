from __future__ import annotations

from typing import Any, Literal, Tuple
import asyncio
import json
import time
from datetime import datetime, timezone

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import DailyIntakeModel, OnboardingAnswerModel
from loguru import logger

# Reuse existing OpenAI Responses API helper
from bot.services.foodai import _openai_request  # type: ignore
from bot.metrics import (
    recommender_started,
    recommender_succeeded,
    recommender_failed,
    recommender_duration_ms,
)

# In-memory recent titles to avoid repeats (MVP). Format: {user_id: [(title, ts), ...]}
_recent_titles: dict[int, list[tuple[str, float]]] = {}


def _get_setting(name: str, default: Any) -> Any:
    try:
        return getattr(settings, name)
    except Exception:
        return default


def _cleanup_recent(user_id: int, window_days: int) -> None:
    now = time.time()
    window = window_days * 86400
    arr = _recent_titles.get(user_id) or []
    _recent_titles[user_id] = [(t, ts) for (t, ts) in arr if now - ts <= window]


def record_recent_title(user_id: int, title: str) -> None:
    window_days = int(_get_setting("REC_AVOID_REPEAT_DAYS", 3) or 3)
    _cleanup_recent(user_id, window_days)
    arr = _recent_titles.get(user_id) or []
    arr.append((title.strip().lower(), time.time()))
    # trim to last 20
    _recent_titles[user_id] = arr[-20:]


def recent_titles(user_id: int) -> list[str]:
    window_days = int(_get_setting("REC_AVOID_REPEAT_DAYS", 3) or 3)
    _cleanup_recent(user_id, window_days)
    return [t for (t, _ts) in (_recent_titles.get(user_id) or [])]


def _is_nutrition_valid(
    data: dict[str, Any] | None,
    target_cal_max: float | None,
    enforce_cap: bool,
) -> tuple[bool, dict[str, float]]:
    """Basic sanity checks for nutrition block.

    Returns (valid, details) where details contains parsed numbers.
    """
    details = {"cal": 0.0, "p": 0.0, "f": 0.0, "c": 0.0}
    if not isinstance(data, dict):
        return False, details
    nutr = data.get("nutrition") or {}
    try:
        cal = float(nutr.get("calories") or 0)
        p = float(nutr.get("protein_g") or 0)
        f = float(nutr.get("fat_g") or 0)
        c = float(nutr.get("carbs_g") or 0)
    except Exception:
        return False, details
    details = {"cal": cal, "p": p, "f": f, "c": c}
    # Hard minimums to avoid zeros
    if cal <= 0 or (p <= 0 and f <= 0 and c <= 0):
        return False, details
    # Rough consistency check: calories ~ 4p + 9f + 4c within ±25%
    try:
        est = 4 * p + 9 * f + 4 * c
        if est > 0:
            rel = abs(cal - est) / est
            if rel > 0.30:  # allow 30% slack
                return False, details
    except Exception:
        pass
    # Optional upper cap guard vs target (only if enforce_cap)
    try:
        if enforce_cap and target_cal_max is not None and cal > (float(target_cal_max) * 1.10):  # allow +10%
            return False, details
    except Exception:
        pass
    return True, details


async def _load_plan_and_fact(user_id: int) -> tuple[dict[str, float], dict[str, float]]:
    """Return (plan, fact) macros for today.
    plan/fact have keys: calories(int), protein_g, fat_g, carbs_g (floats)
    Missing values default to zeros.
    """
    today_utc = datetime.now(timezone.utc).date()
    plan = {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    fact = {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    async with sessionmaker() as session:
        oa = await session.scalar(
            OnboardingAnswerModel.__table__.select().where(OnboardingAnswerModel.user_id == user_id)
        )
        if oa and isinstance(getattr(oa, "daily_plan", None), dict):
            dp = oa.daily_plan or {}
            try:
                plan = {
                    "calories": float(dp.get("calories") or 0),
                    "protein_g": float(dp.get("protein_g") or 0),
                    "fat_g": float(dp.get("fat_g") or 0),
                    "carbs_g": float(dp.get("carbs_g") or 0),
                }
            except Exception:
                pass
        di = await session.scalar(
            DailyIntakeModel.__table__.select().where(
                (DailyIntakeModel.user_id == user_id) & (DailyIntakeModel.date_utc == today_utc)
            )
        )
        if di:
            try:
                fact = {
                    "calories": float(di.calories or 0),
                    "protein_g": float(di.protein_g or 0),
                    "fat_g": float(di.fat_g or 0),
                    "carbs_g": float(di.carbs_g or 0),
                }
            except Exception:
                pass
    return plan, fact


def _build_instructions(meal_type: str, plan: dict[str, float], fact: dict[str, float], avoid: list[str]) -> tuple[str, dict[str, Any]]:
    # Calculate remaining and soft caps
    cal_plan = float(plan.get("calories") or 0)
    cal_fact = float(fact.get("calories") or 0)
    remaining_cal = cal_plan - cal_fact
    overshoot_pct = float(_get_setting("RECOMMENDER_OVERSHOOT_CAL_PCT", 5) or 5)
    target_cal_max = max(0.0, remaining_cal) + cal_plan * overshoot_pct / 100.0

    # Soft goals for macros
    p_plan = float(plan.get("protein_g") or 0)
    p_fact = float(fact.get("protein_g") or 0)
    f_plan = float(plan.get("fat_g") or 0)
    f_fact = float(fact.get("fat_g") or 0)
    c_plan = float(plan.get("carbs_g") or 0)
    c_fact = float(fact.get("carbs_g") or 0)

    p_rem = p_plan - p_fact
    f_rem = f_plan - f_fact
    c_rem = c_plan - c_fact

    # Avoid list formatted
    avoid_list = ", ".join(sorted(set([t for t in avoid if t]))) if avoid else ""

    system = (
        "Ты — ИИ-нутрициолог. Сгенерируй рекомендацию блюда на русском, без markdown. "
        "Формат ответа строго JSON по схеме. Единицы: граммы и ккал. Не упоминай уверенность. "
        "Учитывай текущий прогресс дня и ограничения: калории не должны превышать мягкий лимит. "
        "Белок добираем приоритетно, жиры не раздуваем при переборе. Допустим небольшой овершут калорий (до {overshoot}%)."
    ).format(overshoot=int(overshoot_pct))

    user = (
        "Тип приёма: {typ}. План КБЖУ: {pc} ккал, Б {pp} г, Ж {pf} г, У {pcb} г. "
        "Факт: {fc} ккал, Б {fp} г, Ж {ff} г, У {fcb} г. Остатки: кал {rc}, Б {rp}, Ж {rf}, У {rcb}. "
        "Цель по калориям для блюда: ≤ {tcmax} ккал. Избегать повторов: {avoid}. "
        "Опиши блюдо и порцию в понятных бытовых мерах, можно упрощать."
    ).format(
        typ={"bf": "завтрак", "ln": "обед", "dn": "ужин", "snack": "перекус"}.get(meal_type, "приём пищи"),
        pc=int(cal_plan), pp=round(p_plan, 1), pf=round(f_plan, 1), pcb=round(c_plan, 1),
        fc=int(cal_fact), fp=round(p_fact, 1), ff=round(f_fact, 1), fcb=round(c_fact, 1),
        rc=int(remaining_cal), rp=round(p_rem, 1), rf=round(f_rem, 1), rcb=round(c_rem, 1),
        tcmax=int(target_cal_max), avoid=avoid_list or "—",
    )

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "nutrition": {
                "type": "object",
                "properties": {
                    "calories": {"type": "integer", "minimum": 20},
                    "protein_g": {"type": "number", "minimum": 0.1},
                    "fat_g": {"type": "number", "minimum": 0.1},
                    "carbs_g": {"type": "number", "minimum": 0.1},
                },
                "required": ["calories", "protein_g", "fat_g", "carbs_g"],
                "additionalProperties": False,
            },
            "portion": {"type": "string"},
            "why": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4},
            "cook_time_min": {"type": "integer", "minimum": 1},
            "difficulty": {"type": "string"},
            "recipe_steps": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 6},
            "tips": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
            "meal_type": {"type": "string"},
            "language": {"type": "string"},
        },
        "required": [
            "title", "description", "nutrition", "portion", "why",
            "cook_time_min", "difficulty", "recipe_steps", "tips", "meal_type", "language"
        ],
        "additionalProperties": False,
    }

    format_obj = {
        "type": "json_schema",
        "name": "recommendation",
        "strict": True,
        "schema": schema,
    }

    return system, {"user": user, "format": format_obj, "target_cal_max": target_cal_max}


async def recommend(
    user_id: int,
    meal_type: Literal["bf", "ln", "dn", "snack"],
    *,
    another: bool = False,
) -> Tuple[dict[str, Any] | None, str | None]:
    """Generate a structured recommendation dict via Responses API.
    Returns None on failure.
    """
    model = str(_get_setting("RECOMMENDER_MODEL", "gpt-5-mini") or "gpt-5-mini")
    timeout = int(_get_setting("RECOMMENDER_TIMEOUT", 8) or 8)
    enforce_cap = bool(_get_setting("RECOMMENDER_ENFORCE_CAP", False) or False)
    language_required = str(_get_setting("RECOMMENDER_LANGUAGE_REQUIRED", "ru") or "ru").lower()

    t0 = time.time()
    try:
        recommender_started.labels(meal_type).inc()
    except Exception:
        pass

    plan, fact = await _load_plan_and_fact(user_id)
    avoid = recent_titles(user_id)
    system, payload_extras = _build_instructions(meal_type, plan, fact, avoid)

    # Build Responses payload
    instructions = system
    # Encourage diversity if another=True
    diversity_hint = " Дай другой вариант, отличный от: " + "; ".join(avoid) if (another and avoid) else ""
    user_text = payload_extras["user"] + diversity_hint

    payload = {
        "model": model,
        "instructions": instructions,
        "reasoning": {"effort": getattr(settings, "FOODAI_REASONING_EFFORT", "minimal")},
        "text": {
            "verbosity": getattr(settings, "FOODAI_TEXT_VERBOSITY", "low"),
            "format": payload_extras["format"],
        },
        "max_output_tokens": 900,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                ],
            }
        ],
    }

    try:
        raw = await asyncio.wait_for(_openai_request("responses", payload), timeout=timeout)
        if not raw:
            try:
                recommender_failed.labels(meal_type, "empty").inc()
                recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
            except Exception:
                pass
            return None, "empty"
        # Robust JSON parse: first try direct, then trim to outermost braces if needed
        data: dict[str, Any] | None = None
        try:
            data = json.loads(raw)
        except Exception:
            t = (raw or "").strip()
            s = t.find("{")
            e = t.rfind("}")
            if s != -1 and e != -1 and e > s:
                try:
                    data = json.loads(t[s : e + 1])
                except Exception:
                    data = None
        if not isinstance(data, dict):
            try:
                logger.warning(
                    "recommender_failed | user_id={} | err=json_parse | len={} | head={}",
                    user_id,
                    len(raw or ""),
                    (raw or "").replace("\n", " ")[:200],
                )
            except Exception:
                pass
            try:
                recommender_failed.labels(meal_type, "json_parse").inc()
                recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
            except Exception:
                pass
            return None, "json_parse"
        # Language validation (if model provided language field)
        lang = str(data.get("language") or "").lower().strip()
        if language_required and lang and lang != language_required:
            retry_user_text_lang = user_text + " Ответ строго на русском языке (language='ru')."
            retry_payload_lang = dict(payload)
            retry_payload_lang["input"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": retry_user_text_lang},
                    ],
                }
            ]
            raw_lang = await asyncio.wait_for(_openai_request("responses", retry_payload_lang), timeout=timeout)
            if not raw_lang:
                try:
                    recommender_failed.labels(meal_type, "language").inc()
                    recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
                except Exception:
                    pass
                return None, "language"
            try:
                data = json.loads(raw_lang)
            except Exception:
                t3 = (raw_lang or "").strip()
                s3 = t3.find("{")
                e3 = t3.rfind("}")
                data = json.loads(t3[s3 : e3 + 1]) if (s3 != -1 and e3 != -1 and e3 > s3) else None
            if not isinstance(data, dict) or str(data.get("language") or "").lower().strip() != language_required:
                try:
                    recommender_failed.labels(meal_type, "language").inc()
                    recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
                except Exception:
                    pass
                return None, "language"
        # Nutrition validation – first pass
        ok, det = _is_nutrition_valid(data, payload_extras.get("target_cal_max"), enforce_cap)
        if not ok:
            try:
                logger.warning(
                    "recommender_invalid | user_id={} | meal_type={} | cal={} p={} f={} c={} | tmax={}",
                    user_id,
                    meal_type,
                    det.get("cal"), det.get("p"), det.get("f"), det.get("c"), payload_extras.get("target_cal_max"),
                )
            except Exception:
                pass
            # Single retry with stronger hint
            retry_user_text = (
                user_text
                + " Важное: nutrition.calories/protein_g/fat_g/carbs_g должны быть > 0; "
                + "calories ≈ 4*protein_g + 9*fat_g + 4*carbs_g (±20%). Если превышает лимит ≤ "
                + str(int(payload_extras.get("target_cal_max") or 0))
                + " ккал — уменьшите порцию."
            )
            retry_payload = dict(payload)
            retry_payload["input"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": retry_user_text},
                    ],
                }
            ]
            raw2 = await asyncio.wait_for(_openai_request("responses", retry_payload), timeout=timeout)
            if not raw2:
                try:
                    recommender_failed.labels(meal_type, "invalid").inc()
                    recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
                except Exception:
                    pass
                return None, "invalid"
            data2: dict[str, Any] | None = None
            try:
                data2 = json.loads(raw2)
            except Exception:
                t2 = (raw2 or "").strip()
                s2 = t2.find("{")
                e2 = t2.rfind("}")
                if s2 != -1 and e2 != -1 and e2 > s2:
                    try:
                        data2 = json.loads(t2[s2 : e2 + 1])
                    except Exception:
                        data2 = None
            if not isinstance(data2, dict):
                try:
                    recommender_failed.labels(meal_type, "json_parse").inc()
                    recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
                except Exception:
                    pass
                return None, "json_parse"
            ok2, det2 = _is_nutrition_valid(data2, payload_extras.get("target_cal_max"), enforce_cap)
            if not ok2:
                try:
                    logger.warning(
                        "recommender_invalid_final | user_id={} | meal_type={} | cal={} p={} f={} c={} | tmax={}",
                        user_id,
                        meal_type,
                        det2.get("cal"), det2.get("p"), det2.get("f"), det2.get("c"), payload_extras.get("target_cal_max"),
                    )
                except Exception:
                    pass
                try:
                    recommender_failed.labels(meal_type, "invalid_final").inc()
                    recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
                except Exception:
                    pass
                return None, "invalid_final"
            data = data2
        # Force language and type
        data["meal_type"] = data.get("meal_type") or {"bf": "завтрак", "ln": "обед", "dn": "ужин", "snack": "перекус"}[meal_type]
        data["language"] = "ru"
        title = str(data.get("title") or "").strip()
        if title:
            record_recent_title(user_id, title)
        try:
            recommender_succeeded.labels(meal_type).inc()
            recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
        except Exception:
            pass
        return data, None
    except asyncio.TimeoutError:
        try:
            logger.warning("recommender_timeout | user_id={} | timeout_s={}", user_id, timeout)
        except Exception:
            pass
        try:
            recommender_failed.labels(meal_type, "timeout").inc()
            recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
        except Exception:
            pass
        return None, "timeout"
    except Exception as e:
        try:
            logger.warning("recommender_failed | user_id={} | err={}", user_id, e)
        except Exception:
            pass
        try:
            recommender_failed.labels(meal_type, "other").inc()
            recommender_duration_ms.labels(meal_type).observe((time.time() - t0) * 1000)
        except Exception:
            pass
        return None, "other"
