from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Optional

import aiohttp
from loguru import logger

from bot.core.config import settings


LEVELS = {"sedentary", "light", "moderate", "active", "athlete"}


@dataclass
class ActivityLLMResult:
    level: Optional[str]
    confidence: float
    features: dict[str, Any]
    rationale: Optional[str]
    version: str


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", s)
    return s.strip()


def _coerce_json(s: str) -> Optional[dict]:
    s1 = _strip_code_fences(s)
    try:
        return json.loads(s1)
    except Exception:
        pass
    # Try to locate first {...} block
    m = re.search(r"\{[\s\S]*\}", s1)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


async def classify_activity(text: str, *, lang_hint: Optional[str] = None) -> Optional[ActivityLLMResult]:
    """Classify free-form activity description into 5-level scale using LLM.

    Returns ActivityLLMResult or None on failure. Respects settings flags and timeouts.
    """
    if not settings.ACTIVITY_LLM_ENABLED:
        return None
    if not settings.OPENAI_API_KEY:
        return None

    model = settings.ACTIVITY_LLM_MODEL or "gpt-4o-mini"
    timeout_sec = float(getattr(settings, "ACTIVITY_LLM_TIMEOUT_SEC", 2.5) or 2.5)

    system = (
        "You are a precise classifier of human physical activity level. "
        "Map the user's free-form description to one of: sedentary, light, moderate, active, athlete. "
        "Consider frequency per week, duration, intensity, steps, job type. "
        "Do NOT consider future intentions (words like 'want', 'plan', 'going to'); classify CURRENT activity only. "
        "Output STRICT JSON with keys: level, confidence, features, rationale, version. "
        "- level: one of ['sedentary','light','moderate','active','athlete']\n"
        "- confidence: number in [0,1]\n"
        "- features: object with extracted signals (e.g., {workouts_per_week:int, workout_types:list, avg_session_minutes:int|null, steps_per_day_range:string, job:string|null})\n"
        "- rationale: short string in the same language as input\n"
        "- version: 'v1'\n"
        "Rules: If frequency/duration are missing, be conservative (prefer 'moderate' over 'athlete')."
    )

    user = {
        "role": "user",
        "content": (
            (f"lang: {lang_hint}\n" if lang_hint else "")
            + "activity_description:\n"
            + (text or "").strip()
        ),
    }

    base_url = settings.OPENAI_BASE_URL.strip() if settings.OPENAI_BASE_URL else "https://api.openai.com"
    url = f"{base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 220,
        "messages": [
            {"role": "system", "content": system},
            user,
        ],
    }

    t0 = perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as sess:
            async with sess.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.warning(
                        "activity_llm_http_error | status={} | body={}",
                        resp.status,
                        txt[:300],
                    )
                    return None
                data = await resp.json()
    except asyncio.TimeoutError:
        logger.info("activity_llm_timeout | timeout_s={}", timeout_sec)
        return None
    except Exception as e:
        logger.exception("activity_llm_exception | err={}", e)
        return None
    dur = int((perf_counter() - t0) * 1000)

    try:
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        obj = _coerce_json(content)
        if not isinstance(obj, dict):
            logger.warning("activity_llm_bad_json | content={}...", content[:200])
            return None
        level = str(obj.get("level") or "").strip().lower()
        confidence = float(obj.get("confidence") or 0.0)
        features = obj.get("features") or {}
        rationale = obj.get("rationale") or None
        version = str(obj.get("version") or "v1")
        if level not in LEVELS:
            logger.warning("activity_llm_invalid_level | level={}", level)
            return None
        logger.info(
            "activity_llm_ok | level={} | conf={:.2f} | dur_ms={}",
            level,
            confidence,
            dur,
        )
        return ActivityLLMResult(level=level, confidence=confidence, features=features, rationale=rationale, version=version)
    except Exception as e:
        logger.exception("activity_llm_parse_fail | err={}", e)
        return None
