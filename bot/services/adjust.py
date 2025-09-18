from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Tuple

import aiohttp
from loguru import logger
import re

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


async def _llm_parse_adjustment(text: str, *, lang_hint: Optional[str]) -> Optional[ParsedAdjustment]:
    if not settings.ADJUST_LLM_ENABLED:
        return None
    if not settings.OPENAI_API_KEY:
        return None

    system = (
        "You are a nutrition assistant that EXTRACTS user intents from free-form text to adjust a daily nutrition plan. "
        "Always return your BEST-EFFORT interpretation even if the text is short, colloquial, or has typos. Language may be Russian. "
        "Return STRICT JSON with keys: intents, activity_override, calories, macros, dietary_restrictions, confidence, rationale, version.\n"
        "- intents: array of tags among [lower_calories, raise_calories, keto, low_carb, high_protein, custom_macros, activity_down, activity_up, lactose_free, gluten_free, sugar_free, low_fodmap_candidate, reduce_protein, reduce_fat, increase_fat] \n"
        "- activity_override: null or one of ['sedentary','light','moderate','active','athlete'] when the user clearly states lower/higher daily activity.\n"
        "- calories: {mode: 'absolute'|'delta'|'percent'|null, value: number|null} (delta is +/- calories per day, percent is +/- percent of CURRENT target). Accept formats like '200 ккал', '200 kcal', '+200', '-10%'.\n"
        "- macros: {scheme: 'keto'|'low_carb'|'high_protein'|'balanced'|'custom'|null, custom_target_g: {protein_g:int|null, fat_g:int|null, carbs_g:int|null}|null }\n"
        "- dietary_restrictions: array of tags ['lactose_free','gluten_free','sugar_free','low_fodmap_candidate']\n"
        "- confidence: 0..1 (use 0.3..0.9 for typical short commands) \n"
        "- rationale: short string in the same language as input \n"
        "- version: 'v1' \n"
        "Rules: Do NOT invent numbers. If the user states just preferences/symptoms without explicit numeric targets, set macros.scheme (if applicable) but keep custom_target_g null. "
        "Do NOT change calories unless user states hunger/too much food or explicit change. "
        "Handle typos (e.g., 'ккла'~'ккал', 'углевод'~'углеводы').\n"
        "RU examples (input -> JSON summary):\n"
        "- 'убери углеводы' -> intents:[low_carb], macros.scheme:'low_carb'\n"
        "- 'кето' -> intents:[keto], macros.scheme:'keto'\n"
        "- 'добавь 200 ккал' -> intents:[raise_calories], calories:{mode:'delta', value:200}\n"
        "- 'минус 10%' -> intents:[lower_calories], calories:{mode:'percent', value:-10}\n"
        "- 'мало двигаюсь' -> intents:[activity_down], activity_override:'light'\n"
        "- 'совсем не двигаюсь' -> activity_override:'sedentary'\n"
        "- 'белка 170 жиры 60' -> macros:{scheme:'custom', custom_target_g:{protein_g:170, fat_g:60, carbs_g:null}}\n"
    )

    user = {
        "role": "user",
        "content": (f"lang: {lang_hint}\n" if lang_hint else "") + (text or "").strip(),
    }

    base_url = settings.OPENAI_BASE_URL.strip() if settings.OPENAI_BASE_URL else "https://api.openai.com"
    url = f"{base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.ADJUST_LLM_MODEL or "gpt-4o-mini",
        "temperature": 0.7,
        "max_tokens": 280,
        # Encourage strict JSON in OpenAI Chat Completions
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            user,
        ],
    }

    timeout = float(getattr(settings, "ADJUST_LLM_TIMEOUT_SEC", 2.5) or 2.5)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "adjust_llm_http_error | status={} | body_len={} | body_sha256={}",
                        resp.status,
                        len(body or ""),
                        _privacy_hash(body),
                    )
                    return None
                data = await resp.json()
    except asyncio.TimeoutError:
        logger.info("adjust_llm_timeout | timeout_s={}", timeout)
        return None
    except Exception as e:
        logger.exception("adjust_llm_exception | err={}", e)
        return None

    try:
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        obj = _coerce_json(content)
        if not isinstance(obj, dict):
            logger.warning(
                "adjust_llm_bad_json | content_len={} | content_sha256={}",
                len(content or ""),
                _privacy_hash(content),
            )
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


def _recompute_macros(calories: int, weight_kg: float, scheme: str | None, custom: Optional[dict]) -> Tuple[int, int, int, str]:
    """Return protein_g, fat_g, carbs_g, scheme_used."""
    # floors/ceilings
    prot_min = int(round(max(1.2 * weight_kg, 60)))  # at least 60g
    prot_cap = int(round(2.4 * weight_kg))
    fat_min = int(round(max(0.6 * weight_kg, 30)))   # at least 30g
    carb_min = 20

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
            protein_g = clamp(protein_g - 10, prot_min, prot_cap)
            f_cal = calories - (protein_g * 4 + carbs_g * 4)
            fat_g = max(fat_min, int(round(f_cal / 9)))
        return protein_g, fat_g, carbs_g, used

    if used == "low_carb":
        carbs_g = max(80, carb_min)
        protein_g = clamp(int(round(1.6 * weight_kg)), prot_min, prot_cap)
        f_cal = calories - (protein_g * 4 + carbs_g * 4)
        fat_g = max(fat_min, int(round(f_cal / 9)))
        if fat_g * 9 + protein_g * 4 + carbs_g * 4 > calories:
            # relax carbs to fit
            carbs_g = carb_min
            f_cal = calories - (protein_g * 4 + carbs_g * 4)
            fat_g = max(fat_min, int(round(f_cal / 9)))
        return protein_g, fat_g, carbs_g, used

    if used == "high_protein":
        protein_g = clamp(int(round(2.0 * weight_kg)), prot_min, prot_cap)
        fat_g = fat_min
        c_cal = calories - (protein_g * 4 + fat_g * 9)
        carbs_g = max(carb_min, int(round(c_cal / 4)))
        if protein_g * 4 + fat_g * 9 + carbs_g * 4 > calories:
            # lower protein a bit to fit
            protein_g = clamp(protein_g - 10, prot_min, prot_cap)
            c_cal = calories - (protein_g * 4 + fat_g * 9)
            carbs_g = max(carb_min, int(round(c_cal / 4)))
        return protein_g, fat_g, carbs_g, used

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
        return protein_g, fat_g, carbs_g, used

    # balanced (default): reuse current split roughly 30/30/40
    p_cal = int(round(calories * 0.30))
    f_cal = int(round(calories * 0.30))
    c_cal = calories - p_cal - f_cal
    protein_g = max(prot_min, int(round(p_cal / 4)))
    fat_g = max(fat_min, int(round(f_cal / 9)))
    carbs_g = max(carb_min, int(round(c_cal / 4)))
    return protein_g, fat_g, carbs_g, used


def _apply_adjustment(base_plan: DailyPlan, data: OnboardingData, parsed: ParsedAdjustment) -> Tuple[DailyPlan, str, dict]:
    # 1) activity override
    ao = _activity_override(parsed.activity_override, data)
    payload = data
    if ao is not None:
        try:
            payload = payload.model_copy(update={"activity_level": ao})
        except Exception:
            payload.activity_level = ao  # type: ignore[attr-defined]

    # Recompute plan if activity changed to update TDEE baseline
    plan0 = calculate_daily_plan(payload)
    tdee = float(plan0.tdee)

    # 2) calories change
    # If activity was overridden, use recalculated plan0.calories as baseline; otherwise keep base_plan.calories
    base_cal = plan0.calories if ao is not None else base_plan.calories
    cal_target = _apply_calorie_change(base_cal, tdee, data.goal, parsed.calories or {}) if parsed.calories else base_cal

    # 3) macros scheme
    scheme = None
    custom = None
    if parsed.macros:
        scheme = parsed.macros.get("scheme")
        custom = parsed.macros.get("custom_target_g")
    protein_g, fat_g, carbs_g, used_scheme = _recompute_macros(cal_target, data.weight_kg, scheme, custom)

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

    explanation = " ".join(parts) if parts else "Применил корректировку и сохранил медицински безопасные границы."

    summary = {
        "activity_override": ao.value if ao else None,
        "calories": cal_target,
        "scheme": used_scheme,
    }

    return new_plan, explanation, summary


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


@cached(ttl=86400, namespace="adjust_llm_v2", key_builder=_cache_key)
async def parse_adjustment_cached(user_id: int, text: str, *, lang_hint: Optional[str]) -> Optional[ParsedAdjustment]:
    # Prefer LLM; if it fails/returns None, fallback to heuristic so user always gets a result.
    res = await _llm_parse_adjustment(text, lang_hint=lang_hint)
    if res is None:
        res = parse_adjustment_heuristic(text)
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
