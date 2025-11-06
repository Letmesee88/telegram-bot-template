from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Tuple

import aiohttp
from loguru import logger

from bot.cache.redis import cached
from bot.core.config import settings
from bot.schemas.onboarding import DailyPlan, OnboardingData, ActivityLevel, Goal
from bot.services.plan import (
    calculate_daily_plan,
    ACTIVITY_MULTIPLIERS,
    MAX_DEFICIT_ABS,
    MAX_DEFICIT_FRAC,
    GAIN_MIN_SURPLUS,
    GAIN_MAX_SURPLUS,
)


def _has_explicit_keto(text: str) -> bool:
    s = (text or "").lower()
    return "кето" in s or "keto" in s


def _has_percent(text: str) -> bool:
    return "%" in (text or "")


def _has_kcal(text: str) -> bool:
    s = (text or "").lower()
    return bool(re.search(r"\b(кк?ал|ккал|kcal|калории)\b", s))


def _has_grams_any(text: str) -> bool:
    s = (text or "").lower()
    return bool(re.search(r"\b(г|g)\b", s))


def _infer_strength(text: str) -> str:
    """Return one of 'slight','moderate','strong' based on wording.
    Default is 'moderate'."""
    s = (text or "").lower()
    if re.search(r"\b(чуть|слегка|понемногу|немного)\b", s):
        return "slight"
    if re.search(r"\b(сильно|максимально|очень|по-максимуму)\b", s):
        return "strong"
    # intensifier: every day → push at least moderate
    if re.search(r"кажд(ый|ое)\s+день", s):
        return "moderate"
    return "moderate"


def _apply_strength_defaults(parsed: 'ParsedAdjustment', *, text: str) -> 'ParsedAdjustment':
    """Fill parsed.calories/macros with deterministic numbers derived from strength
    when user did not provide explicit units in the text (Mode B)."""
    if (settings.ADJUST_ENGINE_MODE or "").lower() != "hybrid":
        return parsed

    strength = _infer_strength(text)
    # Map strength to numbers from settings
    cal_map = {
        "slight": settings.ADJUST_STRENGTH_CAL_PERCENT_SLIGHT,
        "moderate": settings.ADJUST_STRENGTH_CAL_PERCENT_MODERATE,
        "strong": settings.ADJUST_STRENGTH_CAL_PERCENT_STRONG,
    }
    carbs_map = {
        "slight": settings.ADJUST_STRENGTH_CARBS_G_SLIGHT,
        "moderate": settings.ADJUST_STRENGTH_CARBS_G_MODERATE,
        "strong": settings.ADJUST_STRENGTH_CARBS_G_STRONG,
    }

    # 1) Calories: if calories intent present but no explicit numbers in text → convert to percent by strength
    if parsed.calories:
        mode = (parsed.calories.get("mode") or "").lower()
        if mode not in {"absolute", "percent", "delta"}:
            mode = ""
        # Apply defaults only when user did not specify any numbers explicitly
        # Using digit check avoids false positives from bare words like "калории"
        if not re.search(r"\d", text or ""):
            # Determine direction from intents
            intents = set(parsed.intents or [])
            if "lower_calories" in intents:
                parsed.calories = {"mode": "percent", "value": -float(cal_map[strength])}
            elif "raise_calories" in intents:
                parsed.calories = {"mode": "percent", "value": +float(cal_map[strength])}
            else:
                parsed.calories = None

    # 2) Macros scheme: keto only if explicit; low_carb baseline carbs by strength when no grams given
    if parsed.macros and isinstance(parsed.macros, dict):
        scheme = parsed.macros.get("scheme")
        cst = parsed.macros.get("custom_target_g")
        # keto gating
        if scheme == "keto" and not _has_explicit_keto(text):
            scheme = "low_carb"
        # low_carb: set carbs target by strength ONLY if user didn't specify grams explicitly in text
        if scheme == "low_carb" and not _has_grams_any(text):
            cst = (cst or {})
            cst["carbs_g"] = int(carbs_map[strength])
        parsed.macros = {"scheme": scheme, "custom_target_g": cst}
    return parsed


def _expand_conversational_heuristics(text: str, pa: Optional['ParsedAdjustment']) -> Optional['ParsedAdjustment']:
    """Augment intents based on simple RU phrases (pizza/sweets/fast food -> low_carb, etc.)."""
    s = (text or "").lower()
    intents = list(pa.intents) if pa else []
    macros = dict(pa.macros) if (pa and pa.macros) else None
    # High refined carbs signs
    if re.search(r"(пицц|сладк|выпечк|булоч|паст[аы]|макарон|лапш|сахар|фастфуд)", s):
        if not macros:
            macros = {"scheme": "low_carb", "custom_target_g": None}
        elif not macros.get("scheme"):
            macros["scheme"] = "low_carb"
        if "low_carb" not in intents:
            intents.append("low_carb")
    # Sedentary signals
    if re.search(r"(ничего\s+не\s+делаю|ничерта\s+не\s+делаю|сиж\w*\s+на\s+диван|почти\s+не\s+двигаюсь|заплыва\w*\s+жиром)", s):
        if "activity_down" not in intents:
            intents.append("activity_down")
        # hint activity override to sedentary
        act_override = "sedentary"
    else:
        act_override = pa.activity_override if pa else None

    if not pa:
        return ParsedAdjustment(intents=intents, activity_override=act_override, calories=None, macros=macros, dietary_restrictions=[], confidence=1.0, rationale=None, version="heuristic-v1")
    pa.intents = intents
    pa.activity_override = act_override or pa.activity_override
    pa.macros = macros or pa.macros
    return pa


@dataclass
class ParsedAdjustment:
    intents: list[str]
    activity_override: Optional[str]
    calories: Optional[dict]
    macros: Optional[dict]
    dietary_restrictions: list[str]
    confidence: float
    rationale: Optional[str]
    version: str


def _privacy_hash(s: str | bytes | None) -> str | None:
    if not s:
        return None
    if isinstance(s, str):
        s = s.encode("utf-8", errors="ignore")
    return hashlib.sha256(s).hexdigest()


def _coerce_json(s: str) -> Optional[dict]:
    # try parse strict first
    try:
        return json.loads(s)
    except Exception:
        pass
    # try to extract first JSON object
    import re

    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _pick_variant(seed: int, options: list[str]) -> str:
    """Select a variant deterministically from a list using a local RNG.
    This avoids global randomness and keeps logs reproducible.
    """
    try:
        import random
        if not options:
            return ""
        rnd = random.Random(seed)
        return rnd.choice(options)
    except Exception:
        return options[0] if options else ""


def _extract_explicit_macros_from_text(text: str) -> dict:
    """Extract explicit grams for protein/fat/carbs from user text.
    Returns dict with optional keys: protein_g, fat_g, carbs_g (ints).
    """
    try:
        import re
        s = (text or "").lower()
        # Normalize decimal comma to dot, then cast to int later
        def _find(patterns: list[tuple[str, str]]) -> Optional[int]:
            for pat, unit in patterns:
                m = re.search(pat, s)
                if m:
                    val = m.group(1).replace(',', '.')
                    try:
                        return int(round(float(val)))
                    except Exception:
                        continue
            return None

        # Patterns: both "углеводы 270 г" and "270 г углеводов"
        carbs = _find([
            (r"(?:углевод\w*|carb\w*)[^0-9]{0,12}(\d+(?:[\.,]\d+)?)\s*г", "g"),
            (r"(\d+(?:[\.,]\d+)?)\s*г[^a-zа-я]{0,12}(?:углевод\w*|carb\w*)", "g"),
        ])
        prot = _find([
            (r"(?:белк\w*|protein\w*)[^0-9]{0,12}(\d+(?:[\.,]\d+)?)\s*г", "g"),
            (r"(\d+(?:[\.,]\d+)?)\s*г[^a-zа-я]{0,12}(?:белк\w*|protein\w*)", "g"),
        ])
        fat = _find([
            (r"(?:жир\w*|fat\w*)[^0-9]{0,12}(\d+(?:[\.,]\d+)?)\s*г", "g"),
            (r"(\d+(?:[\.,]\d+)?)\s*г[^a-zа-я]{0,12}(?:жир\w*|fat\w*)", "g"),
        ])
        out: dict = {}
        if prot is not None:
            out["protein_g"] = prot
        if fat is not None:
            out["fat_g"] = fat
        if carbs is not None:
            out["carbs_g"] = carbs
        return out
    except Exception:
        return {}


def _is_veggies_request(text: str) -> bool:
    try:
        import re
        s = (text or "").lower()
        return bool(re.search(r"(овощ|клетчат|зелень)", s))
    except Exception:
        return False


def _extract_weekly_rate(text: str) -> Optional[float]:
    """Extract weekly rate in kg/week from text, e.g. '0.7 кг в неделю' or '1 кг/нед'."""
    try:
        import re
        s = (text or "").lower()
        m = re.search(r"(\d+(?:[\.,]\d+)?)\s*(кг\s*/\s*нед|кг/нед|кг\s+в\s+нед|кг\s+в\s+неделю)", s)
        if not m:
            m = re.search(r"скорост[ьи]|темп\s*[:\-]?\s*(\d+(?:[\.,]\d+)?)\s*кг", s)
        if m:
            val = (m.group(1) if m.lastindex else m.group(0)).replace(",", ".")
            rate = float(val)
            if rate > 0:
                return rate
    except Exception:
        return None
    return None


def _extract_deadline_date(text: str) -> Optional[str]:
    """Extract ISO date 'YYYY-MM-DD' from phrases like 'к 01.03.2026' or 'к 01.03'."""
    try:
        import re
        from datetime import date as _date
        s = (text or "").lower()
        m = re.search(r"\b(\d{1,2})[\.\/\-](\d{1,2})(?:[\.\/\-](\d{4}))?\b", s)
        if not m:
            return None
        d, mth, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        today = _date.today()
        year = int(yr) if yr else today.year
        try:
            target = _date(year, mth, d)
        except Exception:
            return None
        if not yr:
            # If no year provided and date already passed this year, assume next year
            if target <= today:
                try:
                    target = _date(today.year + 1, mth, d)
                except Exception:
                    return None
        return target.isoformat()
    except Exception:
        return None


def _respect_only_specified(text: str, pa: Optional['ParsedAdjustment']) -> Optional['ParsedAdjustment']:
    """If user specified exactly one macro in grams, force only that macro and drop others from custom_target_g."""
    if not pa:
        return pa
    try:
        exp = _extract_explicit_macros_from_text(text)
        keys = [k for k in ("protein_g", "fat_g", "carbs_g") if k in exp]
        if len(keys) == 1:
            k = keys[0]
            # Ensure macros object
            cst = {k: int(exp[k])}
            pa.macros = {"scheme": "custom", "custom_target_g": cst}
        return pa
    except Exception:
        return pa


async def _llm_parse_adjustment(text: str, *, lang_hint: Optional[str], base_ctx: Optional[dict] = None) -> Optional[ParsedAdjustment]:
    if not settings.ADJUST_LLM_ENABLED:
        return None
    if not settings.OPENAI_API_KEY:
        return None

    system = (
        "Ты — опытный нутрициолог. Получишь BASE_CONTEXT (текущий план и цель) и USER_REQUEST. Верни строго JSON по схеме:"
        " intents, activity_override, calories, macros, dietary_restrictions, confidence, rationale, version.\n"
        "- intents: теги из [lower_calories, raise_calories, keto, low_carb, high_protein, custom_macros, activity_down, activity_up, lactose_free, gluten_free, sugar_free, low_fodmap_candidate, reduce_protein, reduce_fat, increase_fat, advice_only].\n"
        "- activity_override: null или ['sedentary','light','moderate','active','athlete'] при явном указании.\n"
        "- calories: {mode: 'absolute'|'delta'|'percent'|'rate_per_week'|'deadline'|null, value:number|string|null}. percent=±% от base_plan.calories. delta=±ккал. rate_per_week=кг/нед, deadline='YYYY-MM-DD'.\n"
        "- macros: {scheme:'keto'|'low_carb'|'high_protein'|'balanced'|'custom'|null, custom_target_g:{protein_g:int|null, fat_g:int|null, carbs_g:int|null}|null}.\n"
        "- НЕ выдумывай числа. Используй BASE_CONTEXT для вычислений при процентах.\n"
        "Правила:\n"
        "1) Проценты по калориям: 'быстрее похудеть/ускорить' без чисел → calories:{mode:'percent', value:-10}. 'чуть' → -5. 'очень/максимально' → -15.\n"
        "2) Проценты по одному макро: 'углеводы -10%' → macros.scheme='custom', custom_target_g:{carbs_g: round(base_plan.carbs_g*0.9)}. Аналогично для белка/жиров. Другие макро не указывать. Калории в этом случае НЕ заполнять.\n"
        "3) Ровно один макро в граммах/процентах → укажи только его (custom_target_g), остальные null.\n"
        "4) Активность: 'сидячая/программист' → 'sedentary'|'light'; 'тренируюсь часто/3-4 раза' → 'moderate'|'active'|'athlete'. Если запрос только про активность — calories/macros не менять.\n"
        "5) 'кето' только при явном слове 'кето'/'keto'. Овощи/клетчатка/зелень → advice_only.\n"
        "6) Скорость/дедлайн: '0.5 кг/нед' → calories:{mode:'rate_per_week', value:0.5}; 'к 01.03.2026' → calories:{mode:'deadline', value:'2026-03-01'}.\n"
        "7) Похудение: не повышай жир без явного 'increase_fat' (озвучи в rationale).\n"
        "- safety (описать, не применять числа): белок/жир/угли минимум — оставь применение на бэкенд.\n"
        "Примеры:\n"
        "- 'уменьши углеводы на 10%'; base_plan.carbs_g=194 → macros:{scheme:'custom', custom_target_g:{carbs_g:175}}, calories:null\n"
        "- 'хочу быстрее похудеть' → intents:[lower_calories], calories:{mode:'percent', value:-10}\n"
        "- 'к 01.03.2026' → calories:{mode:'deadline', value:'2026-03-01'}\n"
        "- 'сидячая работа' → activity_override:'sedentary', calories:null, macros:null\n"
        "- 'больше овощей' → intents:[advice_only], calories:null, macros:null\n"
    )

    user = {
        "role": "user",
        "content": (f"lang: {lang_hint}\n" if lang_hint else "") + (text or "").strip(),
    }

    # Primary path: OpenAI Responses API with JSON Schema
    base = settings.OPENAI_BASE_URL or "https://api.openai.com/v1"
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"}
    model_id = settings.ADJUST_LLM_MODEL or "gpt-5-mini"
    schema = {
        "type": "object",
        "properties": {
            "intents": {"type": "array", "items": {"type": "string"}},
            "activity_override": {"type": ["string", "null"]},
            "calories": {
                "type": ["object", "null"],
                "properties": {"mode": {"type": ["string","null"]}, "value": {"type": ["number","null"]}},
                "required": ["mode","value"],
                "additionalProperties": False
            },
            "macros": {
                "type": ["object","null"],
                "properties": {
                    "scheme": {"type": ["string","null"]},
                    "custom_target_g": {
                        "type": ["object","null"],
                        "properties": {
                            "protein_g": {"type": ["integer","null"]},
                            "fat_g": {"type": ["integer","null"]},
                            "carbs_g": {"type": ["integer","null"]}
                        },
                        "additionalProperties": False
                    }
                },
                "additionalProperties": False
            },
            "dietary_restrictions": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": ["number","null"], "minimum": 0, "maximum": 1},
            "rationale": {"type": ["string","null"]},
            "version": {"type": ["string","null"]}
        },
        "required": ["intents","activity_override","calories","macros","dietary_restrictions","confidence","rationale","version"],
        "additionalProperties": False
    }
    payload_resp = {
        "model": model_id,
        "instructions": system,
        "max_output_tokens": 600,
        "text": {
            "format": {"type": "json_schema", "name": "adjustment", "strict": True, "schema": schema}
        },
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": ("BASE_CONTEXT:\n" + json.dumps(base_ctx, ensure_ascii=False) if base_ctx else "BASE_CONTEXT:\n{}")},
                    {"type": "input_text", "text": (f"USER_REQUEST:\nlang: {lang_hint}\n" if lang_hint else "USER_REQUEST:\n") + (text or "").strip()},
                ],
            }
        ],
    }
    timeout = float(getattr(settings, "ADJUST_LLM_TIMEOUT_SEC", 4.0) or 4.0)
    data: Optional[dict] = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.post(f"{base}/responses", headers=headers, json=payload_resp) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("adjust_llm_http_error | endpoint=/responses | status={} | body_len={} | body_sha256={}", resp.status, len(body or ""), _privacy_hash(body))
                else:
                    data = await resp.json()
    except asyncio.TimeoutError:
        logger.info("adjust_llm_timeout | endpoint=/responses | timeout_s={}", timeout)
    except Exception as e:
        logger.exception("adjust_llm_exception | endpoint=/responses | err={}", e)

    def _extract_responses_text(d: dict | None) -> Optional[str]:
        if not d:
            return None
        try:
            if isinstance(d.get("output_text"), str) and d["output_text"].strip():
                return d["output_text"].strip()
            out: list[str] = []
            for piece in (d.get("output") or []):
                if (piece or {}).get("type") == "message":
                    for c in (piece.get("content") or []):
                        if (c or {}).get("type") in {"output_text", "input_text", "text"}:
                            t = (c.get("text") or "").strip()
                            if t:
                                out.append(t)
            if out:
                return "\n".join(out)
        except Exception:
            return None
        return None

    content = _extract_responses_text(data)
    if not content:
        # Fallback to Chat Completions with json_object (legacy)
        base_url = settings.OPENAI_BASE_URL.strip() if settings.OPENAI_BASE_URL else "https://api.openai.com"
        url = f"{base_url}/v1/chat/completions"
        payload_chat = {
            "model": getattr(settings, "ADJUST_CHAT_FALLBACK_MODEL", None) or "gpt-4o-mini",
            "temperature": 0.0,
            "max_tokens": 320,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": ("BASE_CONTEXT:\n" + json.dumps(base_ctx, ensure_ascii=False) if base_ctx else "BASE_CONTEXT:\n{}")},
                user,
            ],
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
                async with sess.post(url, headers=headers, json=payload_chat) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("adjust_llm_http_error | endpoint=/chat/completions | status={} | body_len={} | body_sha256={}", resp.status, len(body or ""), _privacy_hash(body))
                        return None
                    data = await resp.json()
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception as e:
            logger.exception("adjust_llm_exception | endpoint=/chat/completions | err={}", e)
            return None

    try:
        obj = _coerce_json(content or "")
        if not isinstance(obj, dict):
            logger.warning("adjust_llm_bad_json | content_len={} | content_sha256={}", len(content or ""), _privacy_hash(content))
            return None
        intents = list(obj.get("intents") or [])
        activity_override = obj.get("activity_override") or None
        calories = obj.get("calories") or None
        macros = obj.get("macros") or None
        dietary = list(obj.get("dietary_restrictions") or [])
        confidence = float(obj.get("confidence") or 0.0)
        rationale = obj.get("rationale") or None
        version = str(obj.get("version") or "v1")
        return ParsedAdjustment(
            intents=intents,
            activity_override=activity_override,
            calories=calories,
            macros=macros,
            dietary_restrictions=dietary,
            confidence=confidence,
            rationale=rationale,
            version=version,
        )
    except Exception as e:
        logger.exception("adjust_llm_parse_fail | err={}", e)
        return None


def _activity_override(level: Optional[str], data: OnboardingData) -> Optional[ActivityLevel]:
    if not level:
        return None
    lvl = str(level).strip().lower()
    if lvl in {"sedentary", "light", "moderate", "active", "athlete"}:
        return ActivityLevel(lvl)
    return None


def _clamp_calories(goal: Goal, tdee: float, target_cal: float) -> int:
    # Safety bounds similar to plan._decide_rate_and_target_cal
    max_deficit = min(MAX_DEFICIT_ABS, MAX_DEFICIT_FRAC * tdee)
    if goal == Goal.lose:
        min_allowed = max(100.0, tdee - max_deficit)
        target_cal = max(min_allowed, min(target_cal, tdee))
    elif goal == Goal.gain:
        min_surplus = GAIN_MIN_SURPLUS
        max_surplus = GAIN_MAX_SURPLUS
        target_cal = max(tdee + min_surplus, min(target_cal, tdee + max_surplus))
    else:
        # maintain: allow +- within general safety
        lower = tdee - max_deficit
        upper = tdee + GAIN_MAX_SURPLUS
        target_cal = max(100.0, min(target_cal, upper))
        target_cal = max(lower, target_cal)
    return int(round(target_cal))


def _calc_weekly_rate(tdee: float, target_cal: int, goal: Goal) -> float:
    delta = (tdee - target_cal) if goal == Goal.lose else (target_cal - tdee)
    if delta <= 0:
        return 0.0
    return round(delta * 7.0 / 7700.0, 2)


def _apply_calorie_change(base_cal: int, tdee: float, goal: Goal, cal_obj: dict) -> int:
    try:
        mode = (cal_obj.get("mode") or "").lower()
        val = cal_obj.get("value")
        if val is None:
            return base_cal
        val = float(val)
        if mode == "absolute":
            target = val
        elif mode == "delta":
            target = base_cal + val
        elif mode == "percent":
            target = base_cal * (1.0 + val / 100.0)
        else:
            return base_cal
        return _clamp_calories(goal, tdee, target)
    except Exception:
        return base_cal


def _normalize_explanation_text(text: str) -> str:
    """Deduplicate repeated sentences, collapse spaces, and cap length (~420 chars)."""
    try:
        import re
        t = (text or "").replace("\n", " ").replace("\r", " ").strip()
        if not t:
            return ""
        # Split into rough sentences by .!? and remove duplicates (case-insensitive)
        parts = [p.strip() for p in re.split(r"[.!?]+", t) if p and p.strip()]
        seen = set()
        uniq: list[str] = []
        for p in parts:
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)
        out = ". ".join(uniq).strip()
        if out and out[-1] not in ".!?":
            out += "."
        # Cap length for caption/UX
        if len(out) > 420:
            out = out[:417].rstrip() + "…"
        return out
    except Exception:
        try:
            return (text or "").strip()
        except Exception:
            return text


def _recompute_macros(
    calories: int,
    weight_kg: float,
    scheme: str | None,
    custom: Optional[dict],
    goal: Goal,
    reduce_fat_requested: bool,
    is_keto_requested: bool,
) -> Tuple[int, int, int, str, bool]:
    """Return protein_g, fat_g, carbs_g, scheme_used, safety_clamped.

    Enforces safety floors:
    - protein >= 1.2 g/kg (lose/maintain) or >= 1.6 g/kg (gain), min 60 g absolute
    - fat >= 0.8 g/kg by default (>= 0.6 g/kg allowed only if explicitly reducing fat), min 30 g absolute
    - carbs >= 100 g/day unless explicit keto, where carbs_min=20 g
    """
    clamped = False

    # floors/ceilings
    prot_floor_per_kg = 1.2 if goal in {Goal.lose, Goal.maintain} else 1.6
    prot_min = int(round(max(prot_floor_per_kg * weight_kg, 60)))  # at least 60g
    prot_cap = int(round(2.4 * weight_kg))

    fat_floor_per_kg = 0.6 if reduce_fat_requested else 0.8
    fat_min = int(round(max(fat_floor_per_kg * weight_kg, 30)))   # at least 30g

    carb_min = 20 if is_keto_requested else 100

    def clamp(v: int, lo: int, hi: int) -> int:
        return max(lo, min(hi, v))

    used = scheme or "balanced"
    if (scheme or "") in {"keto", "low_carb", "high_protein", "balanced", "custom"}:
        used = scheme or "balanced"
    else:
        used = "balanced"

    if used == "keto":
        carbs_g = carb_min
        protein_g = clamp(int(round(1.8 * weight_kg)), prot_min, prot_cap)
        f_cal = calories - (protein_g * 4 + carbs_g * 4)
        fat_g = max(fat_min, int(round(f_cal / 9)))
        if fat_g * 9 + protein_g * 4 + carbs_g * 4 > calories:
            # reduce protein slightly to satisfy fat_min
            new_p = clamp(protein_g - 10, prot_min, prot_cap)
            if new_p != protein_g:
                clamped = True
            protein_g = new_p
            f_cal = calories - (protein_g * 4 + carbs_g * 4)
            fat_new = max(fat_min, int(round(f_cal / 9)))
            if fat_new != fat_g:
                clamped = True
            fat_g = fat_new
        return protein_g, fat_g, carbs_g, used, clamped

    if used == "low_carb":
        carbs_g = max(100, carb_min)
        protein_g = clamp(int(round(1.6 * weight_kg)), prot_min, prot_cap)
        f_cal = calories - (protein_g * 4 + carbs_g * 4)
        fat_new = max(fat_min, int(round(f_cal / 9)))
        fat_g = fat_new
        if fat_g * 9 + protein_g * 4 + carbs_g * 4 > calories:
            # relax carbs to fit
            carbs_new = carb_min
            if carbs_new != carbs_g:
                clamped = True
            carbs_g = carbs_new
            f_cal = calories - (protein_g * 4 + carbs_g * 4)
            fat_g = max(fat_min, int(round(f_cal / 9)))
        return protein_g, fat_g, carbs_g, used, clamped

    if used == "high_protein":
        protein_g = clamp(int(round(2.0 * weight_kg)), prot_min, prot_cap)
        fat_g = fat_min
        c_cal = calories - (protein_g * 4 + fat_g * 9)
        carbs_g = max(carb_min, int(round(c_cal / 4)))
        if protein_g * 4 + fat_g * 9 + carbs_g * 4 > calories:
            # lower protein a bit to fit
            new_p = clamp(protein_g - 10, prot_min, prot_cap)
            if new_p != protein_g:
                clamped = True
            protein_g = new_p
            c_cal = calories - (protein_g * 4 + fat_g * 9)
            carbs_g = max(carb_min, int(round(c_cal / 4)))
        return protein_g, fat_g, carbs_g, used, clamped

    if used == "custom" and isinstance(custom, dict):
        p = custom.get("protein_g")
        f = custom.get("fat_g")
        c = custom.get("carbs_g")
        protein_g = clamp(int(p) if p is not None else prot_min, prot_min, prot_cap)
        fat_g = max(fat_min, int(f) if f is not None else fat_min)
        # allocate remainder to carbs if not specified
        if c is None:
            c_cal = calories - (protein_g * 4 + fat_g * 9)
            carbs_g = max(carb_min, int(round(c_cal / 4)))
        else:
            carbs_g = max(carb_min, int(c))
        # final normalization: if sum exceeds, reduce carbs first
        total = protein_g * 4 + fat_g * 9 + carbs_g * 4
        if total > calories:
            excess_cal = total - calories
            reduce_c = min(carbs_g - carb_min, int(round(excess_cal / 4)))
            carbs_g -= max(0, reduce_c)
        return protein_g, fat_g, carbs_g, used, (protein_g < (p or protein_g) or fat_g < (f or fat_g) or c is not None and carbs_g < int(c))

    # balanced (default): reuse current split roughly 30/30/40
    p_cal = int(round(calories * 0.30))
    f_cal = int(round(calories * 0.30))
    c_cal = calories - p_cal - f_cal
    protein_g = max(prot_min, int(round(p_cal / 4)))
    fat_g = max(fat_min, int(round(f_cal / 9)))
    carbs_g = max(carb_min, int(round(c_cal / 4)))
    if protein_g == prot_min or fat_g == fat_min or carbs_g == carb_min:
        clamped = True
    return protein_g, fat_g, carbs_g, used, clamped


def _apply_adjustment(base_plan: DailyPlan, data: OnboardingData, parsed: ParsedAdjustment) -> Tuple[DailyPlan, str, dict]:
    # 1) activity override
    ao = _activity_override(parsed.activity_override, data)
    payload = data
    if ao is not None:
        try:
            payload = payload.model_copy(update={"activity_level": ao})
        except Exception:
            payload.activity_level = ao  # type: ignore[attr-defined]

    # Advice-only guard: нерелевантные запросы — план не менять, дать только совет
    intents_set = set(parsed.intents or [])
    if ("advice_only" in intents_set) and (not parsed.activity_override) and (not parsed.calories) and (not parsed.macros):
        explanation = (parsed.rationale or "Продолжай активность: в тренировочные дни чуть увеличивай углеводы, следи за водой и электролитами.")
        summary = {"activity_override": None, "calories": base_plan.calories, "scheme": None, "advice_only": True}
        return base_plan, explanation, summary

    # Recompute plan if activity changed to update TDEE baseline
    plan0 = calculate_daily_plan(payload)
    tdee = float(plan0.tdee)

    # 2) calories change
    # IMPORTANT: Do not change calories when only activity changes; keep user's base calories
    base_cal = base_plan.calories
    # Apply calories with support for special modes (weekly rate / deadline date)
    if parsed.calories:
        mode = str((parsed.calories.get("mode") or "")).lower()
        val = parsed.calories.get("value")
        if mode in {"absolute", "delta", "percent"}:
            cal_target = _apply_calorie_change(base_cal, tdee, data.goal, parsed.calories or {})
        elif mode == "rate_per_week":
            try:
                rate = max(0.0, float(val or 0.0))
                if rate > 0:
                    delta = rate * 7700.0 / 7.0
                    target = (tdee - delta) if data.goal == Goal.lose else (tdee + delta)
                    cal_target = _clamp_calories(data.goal, tdee, target)
                else:
                    cal_target = base_cal
            except Exception:
                cal_target = base_cal
        elif mode in {"deadline", "deadline_date"}:
            try:
                from datetime import date as _date
                if not data.goal_weight_kg or not data.weight_kg:
                    cal_target = base_cal
                else:
                    iso = (val or "").strip()
                    target_date = _date.fromisoformat(iso)
                    today = _date.today()
                    days = max(1, (target_date - today).days)
                    weeks = max(0.1, days / 7.0)
                    kg_left = abs(float(data.weight_kg) - float(data.goal_weight_kg))
                    if kg_left <= 0:
                        cal_target = base_cal
                    else:
                        rate = kg_left / weeks
                        delta = rate * 7700.0 / 7.0
                        target = (tdee - delta) if data.goal == Goal.lose else (tdee + delta)
                        cal_target = _clamp_calories(data.goal, tdee, target)
            except Exception:
                cal_target = base_cal
        else:
            cal_target = base_cal
    else:
        cal_target = base_cal

    # 3) macros scheme
    scheme = None
    custom = None
    single_macro_requested = False
    if parsed.macros:
        scheme = parsed.macros.get("scheme")
        custom = parsed.macros.get("custom_target_g")
        if scheme == "custom" and isinstance(custom, dict):
            try:
                specified = sum(1 for k in ("protein_g","fat_g","carbs_g") if custom.get(k) is not None)
                single_macro_requested = (specified == 1)
            except Exception:
                single_macro_requested = False

    # Mode B: partial custom — keep unspecified macros from current plan; carbs as remainder when None
    if scheme == "custom" and isinstance(custom, dict):
        if custom.get("protein_g") is None:
            custom["protein_g"] = int(base_plan.protein_g)
        if custom.get("fat_g") is None:
            custom["fat_g"] = int(base_plan.fat_g)
        # If only one macro was explicitly requested, don't allocate remainder: keep carbs at base
        if single_macro_requested:
            if custom.get("carbs_g") is None:
                custom["carbs_g"] = int(base_plan.carbs_g)
        # else: if carbs missing -> keep None to allocate remainder below in _recompute_macros

    # Low-carb by strength may set only carbs_g; keep other macros from current plan to avoid jumps
    if scheme == "low_carb" and isinstance(custom, dict) and custom.get("carbs_g") is not None:
        if custom.get("protein_g") is None:
            custom["protein_g"] = int(base_plan.protein_g)
        if custom.get("fat_g") is None:
            custom["fat_g"] = int(base_plan.fat_g)

    intents_set = set(parsed.intents or [])
    reduce_fat_req = ("reduce_fat" in intents_set)
    is_keto_req = ((scheme or "") == "keto") or ("keto" in intents_set)
    increase_fat_req = ("increase_fat" in intents_set)

    # If user didn't request calories/macros changes, keep existing macros unchanged
    if (parsed.macros is None) and (parsed.calories is None):
        src_plan = plan0 if ao is not None else base_plan
        protein_g, fat_g, carbs_g = int(src_plan.protein_g), int(src_plan.fat_g), int(src_plan.carbs_g)
        used_scheme, clamped = None, False
    else:
        protein_g, fat_g, carbs_g, used_scheme, clamped = _recompute_macros(
            cal_target,
            data.weight_kg,
            scheme,
            custom,
            data.goal,
            reduce_fat_req,
            is_keto_req,
        )

    # single_macro_requested was computed before augmentation

    # Additional guard: during weight loss, do not increase fat above base without explicit request
    if data.goal == Goal.lose and (not increase_fat_req) and fat_g > int(base_plan.fat_g):
        base_fat_val = int(base_plan.fat_g)
        fat_g = base_fat_val
        if not single_macro_requested:
            # Try to reallocate to carbs only if it doesn't contradict low_carb intent; otherwise allow slight extra deficit
            carb_min = 20 if is_keto_req else 100
            # maintain within cal_target when possible, but do not increase carbs if explicitly low_carb was requested and carbs would rise
            c_cal = int(cal_target) - (int(protein_g) * 4 + int(fat_g) * 9)
            carbs_new = max(carb_min, int(round(max(0, c_cal) / 4)))
            if ('low_carb' in intents_set) and carbs_new > carbs_g:
                # keep carbs as is; accept extra deficit
                pass
            else:
                if carbs_new != carbs_g:
                    clamped = True
                    carbs_g = carbs_new

    # If user fixed exactly one macro via custom, let calories drop instead of compensating with other macros
    if single_macro_requested:
        total_cal = int(protein_g) * 4 + int(fat_g) * 9 + int(carbs_g) * 4
        if total_cal < int(cal_target):
            cal_target = total_cal

    # 4) weekly rate (rough) and eta keep from plan0 when possible
    weekly_rate_kg = _calc_weekly_rate(tdee, cal_target, data.goal)

    new_plan = DailyPlan(
        calories=int(cal_target),
        protein_g=int(protein_g),
        fat_g=int(fat_g),
        carbs_g=int(carbs_g),
        sources=plan0.sources,
        tdee=int(round(tdee)),
        weekly_rate_kg=weekly_rate_kg,
        eta_date=plan0.eta_date,  # keep old ETA; precise recompute could be added later
    )

    # Build detailed human explanation
    act_ru = None
    if ao is not None:
        amap = {
            "sedentary": "очень низкой",
            "light": "низкой",
            "moderate": "умеренной",
            "active": "высокой",
            "athlete": "очень высокой",
        }
        act_ru = amap.get(ao.value, ao.value)

    cal_dir = "сохранил"
    if new_plan.calories > base_cal:
        cal_dir = "увеличил"
    elif new_plan.calories < base_cal:
        cal_dir = "снизил"

    scheme_note = None
    if used_scheme and used_scheme != "balanced":
        if used_scheme == "keto":
            scheme_note = "схему кето (≈20 г углеводов, повышенные жиры)"
        elif used_scheme == "low_carb":
            scheme_note = "низкоуглеводную схему"
        elif used_scheme == "high_protein":
            scheme_note = "акцент на белок"
        elif used_scheme == "custom":
            scheme_note = "заданные вручную целевые граммы макронутриентов"

    # Seed for textual variety (user-specific + time-based)
    try:
        seed_base = (getattr(data, "user_id", 0) or 0)
    except Exception:
        seed_base = 0

    parts: list[str] = []
    # Контекст активности
    if act_ru:
        v = _pick_variant(seed_base ^ 1, [
            f"С учётом {act_ru} физической активности скорректировал план питания.",
            f"Учёл, что твоя активность {act_ru}, и подправил план.",
            f"План адаптирован под {act_ru} уровень активности.",
        ])
        if v:
            parts.append(v)

    # Контекст цели и калорий (варианты формулировок, числа сохраняем)
    if data.goal == Goal.lose:
        v = _pick_variant(seed_base ^ 2, [
            f"Чтобы держать умеренный дефицит без риска потери мышечной массы и чрезмерного стресса для метаболизма, {cal_dir} калорийность до {new_plan.calories} ккал.",
            f"Сохраняю безопасный умеренный дефицит: {cal_dir} калории до {new_plan.calories} ккал — без риска для мышц и метаболизма.",
            f"Поддерживаю умеренный дефицит, чтобы худеть без перегруза: {cal_dir} калорийность до {new_plan.calories} ккал.",
        ])
        parts.append(v)
    elif data.goal == Goal.gain:
        v = _pick_variant(seed_base ^ 3, [
            f"Для набора — важно умеренно повысить калорийность. Поэтому {cal_dir} калорийность до {new_plan.calories} ккал.",
            f"Для набора массы ставлю умеренный профицит: {cal_dir} калории до {new_plan.calories} ккал.",
            f"Поддерживаю аккуратный профицит, {cal_dir} калорийность до {new_plan.calories} ккал.",
        ])
        parts.append(v)
    else:
        v = _pick_variant(seed_base ^ 4, [
            f"{cal_dir.capitalize()} калорийность до {new_plan.calories} ккал, сохранив медицински безопасные границы.",
            f"Целевые калории {cal_dir} до {new_plan.calories} ккал в рамках безопасного диапазона.",
            f"Оставляю калории в безопасной зоне: {cal_dir} до {new_plan.calories} ккал.",
        ])
        parts.append(v)

    # Скорость
    if new_plan.weekly_rate_kg and new_plan.weekly_rate_kg > 0:
        v = _pick_variant(seed_base ^ 5, [
            f"Это соответствует примерно {new_plan.weekly_rate_kg} кг в неделю.",
            f"Ориентировочная скорость — {new_plan.weekly_rate_kg} кг/нед.",
            f"Примерная динамика: {new_plan.weekly_rate_kg} кг за неделю.",
        ])
        parts.append(v)

    # Макросы
    if scheme_note:
        v = _pick_variant(seed_base ^ 6, [
            f"По макросам применил {scheme_note}. Белок выставлен на уровне {new_plan.protein_g} г для поддержки мышц; жиры — {new_plan.fat_g} г, углеводы — {new_plan.carbs_g} г (не опускаем ниже безопасного минимума).",
            f"С макро — {scheme_note}: белок {new_plan.protein_g} г (поддержка мышц), жиры {new_plan.fat_g} г, углеводы {new_plan.carbs_g} г.",
            f"Выбрал {scheme_note}; цели по БЖУ: белки {new_plan.protein_g} г, жиры {new_plan.fat_g} г, углеводы {new_plan.carbs_g} г.",
        ])
        parts.append(v)
    else:
        v = _pick_variant(seed_base ^ 7, [
            f"Баланс макро нормализован: белки {new_plan.protein_g} г, жиры {new_plan.fat_g} г, углеводы {new_plan.carbs_g} г.",
            f"По макросам: белки — {new_plan.protein_g} г; жиры — {new_plan.fat_g} г; углеводы — {new_plan.carbs_g} г.",
            f"Цели по БЖУ: {new_plan.protein_g} г / {new_plan.fat_g} г / {new_plan.carbs_g} г.",
        ])
        parts.append(v)

    # Диетологические заметки
    if getattr(parsed, "dietary_restrictions", None):
        dr = set(parsed.dietary_restrictions or [])
        if "lactose_free" in dr:
            parts.append("Если молочные продукты вызывают дискомфорт — используйте безлактозные/растительные аналоги.")
        if "low_fodmap_candidate" in dr:
            parts.append("Чтобы уменьшить вздутие, временно ограничьте продукты с высоким FODMAP (бобовые, капустные, лук/чеснок, газировку).")

    # Prefer deterministic explanation when the visible plan changed for the user.
    # Compare against the user's current plan before adjustment (base_plan), not plan0.
    changed_vs_user = (
        int(new_plan.calories) != int(base_plan.calories)
        or int(new_plan.protein_g) != int(base_plan.protein_g)
        or int(new_plan.fat_g) != int(base_plan.fat_g)
        or int(new_plan.carbs_g) != int(base_plan.carbs_g)
    )
    explanation_text = ""
    if not changed_vs_user:
        # Only trust LLM rationale if nothing changed numerically for the user
        explanation_text = (parsed.rationale or "").strip() if getattr(parsed, "rationale", None) else ""
    if not explanation_text:
        explanation_text = " ".join(parts) if parts else "Применил корректировку и сохранил медицински безопасные границы."
    safety_clamped = bool(clamped)
    if safety_clamped:
        explanation_text = (explanation_text + " ").strip() + "Сохранены безопасные границы по белкам/жирам/углеводам."
    # Deduplicate repeated sentences and cap length for UX
    explanation_text = _normalize_explanation_text(explanation_text)

    summary = {
        "activity_override": ao.value if ao else None,
        "calories": cal_target,
        "scheme": used_scheme,
        "safety_clamped": safety_clamped,
    }

    return new_plan, explanation_text, summary


def apply_adjustment(base_plan: DailyPlan, data: OnboardingData, parsed: ParsedAdjustment) -> Tuple[DailyPlan, str, dict]:
    return _apply_adjustment(base_plan, data, parsed)


# --- Heuristic fallback (regex-based) ---
def parse_adjustment_heuristic(text: str) -> Optional[ParsedAdjustment]:
    """Very small RU-oriented heuristic to cover top intents offline.

    Supports:
    - low_carb/keto by phrases like 'убери углеводы', 'кето'
    - calories +/- N by 'добавь 200 ккал', 'минус 300 ккал', '−10%'
    - activity override: 'мало двигаюсь'/'низкая активность' → light; 'совсем не двигаюсь'/'сидячая' → sedentary
    """
    if not text:
        return None
    s = text.lower().strip()

    intents: list[str] = []
    activity_override: Optional[str] = None
    calories: Optional[dict] = None
    macros: Optional[dict] = None
    dietary: list[str] = []

    # macros: keto/low_carb
    if "кето" in s or "keto" in s:
        intents.append("keto")
        macros = {"scheme": "keto", "custom_target_g": None}
    elif re.search(r"(убер(и|ите)|меньше|сниз(ь|ьте)|исключ(и|ите)|убрать|уменьш(и|ите)).*(углевод|угли|carb)", s):
        intents.append("low_carb")
        macros = {"scheme": "low_carb", "custom_target_g": None}

    # calories: +/- N kcal
    m = re.search(r"(?P<verb>добав(ь|ьте)|прибавь|плюс|увелич(ь|ьте)|минус|убав(ь|ьте)|сниз(ь|ьте)|уменьш(ь|йте))\s*(?P<num>\d{2,5})\s*(кк?ал|ккла|ккал|кал|kcal|калории)", s)
    if m:
        num = float(m.group("num"))
        verb = m.group("verb") or ""
        sign = +1.0 if re.search(r"добав|прибав|плюс|увелич", verb) else -1.0
        intents.append("raise_calories" if sign > 0 else "lower_calories")
        calories = {"mode": "delta", "value": sign * num}
    else:
        # percent like '10%'
        m2 = re.search(r"(?P<verb>добав(ь|йте)|прибавь|плюс|увелич(ь|йте)|минус|убав(ь|йте)|сниз(ь|йте)|уменьш(ь|йте)).*?(?P<num>\d{1,2})\s*%", s)
        if m2:
            num = float(m2.group("num"))
            verb = m2.group("verb") or ""
            sign = +1.0 if re.search(r"добав|прибав|плюс|увелич", verb) else -1.0
            intents.append("raise_calories" if sign > 0 else "lower_calories")
            calories = {"mode": "percent", "value": sign * num}

    # activity override
    if re.search(r"(совсем\s+не\s+двигаюсь|совсем\s+не\s+двигаюсь|сидяч(ий|ая)|почти\s+не\s+двигаюсь)", s):
        activity_override = "sedentary"
        intents.append("activity_down")
    elif re.search(r"(мало\s+двигаюсь|низкая\s+актив|меньше\s+двигаюсь)", s):
        activity_override = "light"
        intents.append("activity_down")
    elif re.search(r"(очень\s+актив|каждый\s+день\s+трен|высокая\s+актив)", s):
        activity_override = "active"
        intents.append("activity_up")

    # explicit grams for macros (respect-only-specified pathway for offline mode)
    grams = _extract_explicit_macros_from_text(text)
    if grams:
        # Keep only those explicitly provided; do not invent others
        macros = {"scheme": "custom", "custom_target_g": grams}

    # sports / non-plan advice-only
    if re.search(r"(игра(ть)?\s+в\s+футбол|в\s+футбол|бегать|пробежк|в\s+зал|тренировк|спорт)", s):
        if "advice_only" not in intents:
            intents.append("advice_only")
    # veggies/fiber → advice_only
    if _is_veggies_request(text):
        if "advice_only" not in intents:
            intents.append("advice_only")

    # GI / lactose signals (no KБЖУ change; only flags and explanation)
    if re.search(r"(пукаю|газ|вздут|метеоризм)", s):
        dietary.append("low_fodmap_candidate")
    if re.search(r"(лактоз|молок|йогурт|сыр)", s):
        dietary.append("lactose_free")

    if not intents and not calories and not macros and not activity_override:
        return None

    return ParsedAdjustment(
        intents=intents,
        activity_override=activity_override,
        calories=calories,
        macros=macros,
        dietary_restrictions=dietary,
        confidence=1.0,
        rationale=None,
        version="heuristic-v1",
    )


def _cache_key(user_id: int, text: str, lang_hint: Optional[str] = None) -> str:
    h = _privacy_hash(text) or "0"
    return f"user={user_id}:t={h}"


def _cache_key(user_id: int, text: str, lang_hint: Optional[str] = None, plan_key: Optional[str] = None, base_ctx: Optional[dict] = None) -> str:
    h = _privacy_hash(text) or "0"
    pk = (plan_key or "0")
    return f"user={user_id}:t={h}:plan={pk}"


@cached(ttl=3600, namespace="adjust_llm_v3", key_builder=_cache_key)
async def parse_adjustment_cached(user_id: int, text: str, *, lang_hint: Optional[str], plan_key: Optional[str] = None, base_ctx: Optional[dict] = None) -> Optional[ParsedAdjustment]:
    # LLM-only mode: no local parsers/post-processing; rely on prompt rules and BASE_CONTEXT
    llm_only = (str(getattr(settings, "ADJUST_ENGINE_MODE", "")).lower() == "llm_only")
    res = await _llm_parse_adjustment(text, lang_hint=lang_hint, base_ctx=base_ctx)
    if not llm_only:
        if res is None:
            res = parse_adjustment_heuristic(text)
        if res is not None:
            res = _expand_conversational_heuristics(text, res)
            res = _apply_strength_defaults(res, text=text)
            res = _respect_only_specified(text, res)
            if _is_veggies_request(text):
                try:
                    intents = set(getattr(res, 'intents', []) or [])
                    intents.add('advice_only')
                    res.intents = list(intents)
                    res.activity_override = None
                    res.calories = None
                    res.macros = None
                except Exception:
                    pass
            try:
                if res:
                    has_keto_word = _has_explicit_keto(text)
                    intents2 = set(getattr(res, 'intents', []) or [])
                    if ('keto' in intents2) and not has_keto_word:
                        intents2.discard('keto')
                        intents2.add('low_carb')
                        res.intents = list(intents2)
                    m = getattr(res, 'macros', None)
                    if isinstance(m, dict) and (m.get('scheme') == 'keto') and not has_keto_word:
                        m['scheme'] = 'low_carb'
                        res.macros = m
            except Exception:
                pass
            try:
                has_cal = isinstance(getattr(res, 'calories', None), dict) and (res.calories or {}).get('mode')
                if not has_cal:
                    rate = _extract_weekly_rate(text)
                    if rate:
                        res.calories = {"mode": "rate_per_week", "value": float(rate)}
                    else:
                        deadline = _extract_deadline_date(text)
                        if deadline:
                            res.calories = {"mode": "deadline", "value": deadline}
            except Exception:
                pass
    return res


# -----------------------------
# Explanation Rephrase (Hybrid)
# -----------------------------

def _extract_numbers_units(s: str) -> list[tuple[str, str]]:
    """Extract ordered pairs (value, unit) where unit in {kcal,g} (ru/en variants).
    Only pairs with explicit units are validated to reduce false positives.
    """
    if not s:
        return []
    text = s.lower()
    # Normalize comma decimal to dot
    text = re.sub(r"(\d),(\d)", r"\1.\2", text)
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*(ккал|кк?ал|калории|kcal)", "kcal"),
        (r"(\d+(?:\.\d+)?)\s*(г|g)\b", "g"),
    ]
    out: list[tuple[str, str]] = []
    for pat, unit in patterns:
        for m in re.finditer(pat, text):
            out.append((m.group(1), unit))
    return out


def _numbers_unchanged(src: str, dst: str) -> bool:
    return _extract_numbers_units(src) == _extract_numbers_units(dst)


def _rephrase_cache_key(text: str, tone: str) -> str:
    h = _privacy_hash(text) or "0"
    return f"text={h}:tone={tone}"


async def _rephrase_explanation(text: str, *, tone: str, timeout: float) -> Optional[str]:
    if not settings.OPENAI_API_KEY:
        return None
    base_url = settings.OPENAI_BASE_URL.strip() if settings.OPENAI_BASE_URL else "https://api.openai.com"
    url = f"{base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    system = (
        "You are a professional Russian editor. Rewrite the user's explanation in the specified tone, "
        "keeping ALL numbers and units EXACTLY the same. Do NOT add or remove any numbers or units. "
        "Return a single concise paragraph in Russian."
    )
    user = {
        "role": "user",
        "content": f"tone: {tone}\n\n{text}",
    }
    payload = {
        "model": settings.ADJUST_LLM_MODEL or "gpt-4o-mini",
        "temperature": 0.5,
        "max_tokens": 160,
        "messages": [
            {"role": "system", "content": system},
            user,
        ],
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "adjust.rephrase.http_error | status={} | body_len={} | body_sha256={}",
                        resp.status,
                        len(body or ""),
                        _privacy_hash(body),
                    )
                    return None
                data = await resp.json()
                content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return content.strip()
    except asyncio.TimeoutError:
        logger.info("adjust.rephrase.timeout | timeout_s={}", timeout)
        return None
    except Exception as e:
        logger.exception("adjust.rephrase.exception | err={}", e)
        return None


@cached(ttl=86400, namespace="adjust_rephrase", key_builder=_rephrase_cache_key)
async def rephrase_explanation_cached(text: str, tone: str = "neutral") -> Optional[str]:
    if not settings.ADJUST_REPHRASE_ENABLED:
        return None
    if not text or len(text) < int(getattr(settings, "ADJUST_REPHRASE_LENGTH_MIN", 220) or 220):
        return None
    timeout = float(getattr(settings, "ADJUST_REPHRASE_TIMEOUT_SEC", 1.8) or 1.8)
    logger.info("adjust.rephrase.sent | tone={} | len={}", tone, len(text))
    rewritten = await _rephrase_explanation(text, tone=tone, timeout=timeout)
    if not rewritten:
        return None
    if not _numbers_unchanged(text, rewritten):
        logger.warning("adjust.rephrase.invalid_numbers | len_src={} | len_dst={}", len(text), len(rewritten))
        return None
    return rewritten
