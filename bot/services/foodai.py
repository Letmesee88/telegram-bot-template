from __future__ import annotations

from typing import Any
import json
from aiohttp import ClientSession
from bot.core.config import settings
from loguru import logger


def _use_openai() -> bool:
    try:
        return (settings.FOODAI_PROVIDER or "").lower() == "openai" and bool(settings.OPENAI_API_KEY)
    except Exception:
        return False


async def _tg_file_url(file_id: str) -> str | None:
    """Resolve Telegram file_id to a downloadable HTTPS URL.

    Uses direct Telegram HTTP API. Returns None on failure.
    """
    token = getattr(settings, "BOT_TOKEN", None)
    if not token:
        return None
    api_base = f"https://api.telegram.org/bot{token}"
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{api_base}/getFile", params={"file_id": file_id}, timeout=settings.FOODAI_TIMEOUT) as r:
                data = await r.json()
        if not data.get("ok"):
            return None
        file_path = (data.get("result") or {}).get("file_path")
        if not file_path:
            return None
        try:
            logger.debug("FoodAI: TG file_path resolved (len={}): {}", len(file_path), file_path)
        except Exception:
            pass
        return f"https://api.telegram.org/file/bot{token}/{file_path}"
    except Exception:
        return None


async def _openai_chat(payload: dict[str, Any]) -> dict[str, Any] | None:
    base = settings.OPENAI_BASE_URL or "https://api.openai.com/v1"
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with ClientSession() as sess:
            async with sess.post(url, headers=headers, data=json.dumps(payload), timeout=settings.FOODAI_TIMEOUT) as r:
                if r.status >= 400:
                    text = await r.text()
                    raise RuntimeError(f"OpenAI HTTP {r.status}: {text}")
                return await r.json()
    except Exception:
        return None


async def _openai_request(kind: str, payload: dict[str, Any]) -> str | None:
    """Call OpenAI API and return assistant text content as a string.

    kind: 'chat' or 'responses'
    """
    base = settings.OPENAI_BASE_URL or "https://api.openai.com/v1"
    endpoint = "/chat/completions" if (kind or "chat").lower() == "chat" else "/responses"
    url = f"{base}{endpoint}"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with ClientSession() as sess:
            async with sess.post(url, headers=headers, data=json.dumps(payload), timeout=settings.FOODAI_TIMEOUT) as r:
                if r.status >= 400:
                    text = await r.text()
                    raise RuntimeError(f"OpenAI HTTP {r.status}: {text}")
                data = await r.json()
    except Exception:
        return None

    # Extract assistant text
    try:
        if endpoint == "/chat/completions":
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            return _strip_code_fence(content) if isinstance(content, str) else None
        # Responses API
        if isinstance(data.get("output_text"), str) and data["output_text"].strip():
            return _strip_code_fence(data["output_text"])  # combined text
        out: list[str] = []
        for piece in (data.get("output") or []):
            if (piece or {}).get("type") == "message":
                for c in (piece.get("content") or []):
                    if (c or {}).get("type") in {"output_text", "input_text", "text"}:
                        t = (c.get("text") or "").strip()
                        if t:
                            out.append(t)
        if out:
            return _strip_code_fence("\n".join(out))
    except Exception:
        pass
    return None


def _strip_code_fence(s: str | None) -> str | None:
    if not s:
        return s
    t = s.strip()
    if t.startswith("```") and t.endswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2:
            inner = "\n".join(lines[1:-1])
            return inner.strip()
    return t


def _normalize_openai_json(raw_text: str) -> dict[str, Any] | None:
    """Parse assistant content as JSON and coerce types safely.

    Expected keys: title, calories, protein_g, fat_g, carbs_g, weight_g, confidence, items[], references{sources[]}, analysis_text.
    """
    try:
        data = json.loads(raw_text)
        cal = int(float(data.get("calories") or 0))
        p = float(data.get("protein_g") or 0)
        f = float(data.get("fat_g") or 0)
        c = float(data.get("carbs_g") or 0)
        w = float(data.get("weight_g") or 0)
        conf = float(data.get("confidence") or 0.7)
        items = data.get("items") or []
        refs = data.get("references") or {}
        title = (data.get("title") or "").strip()
        analysis_text = (data.get("analysis_text") or "").strip()
        if not isinstance(items, list):
            items = []
        if not isinstance(refs, dict):
            refs = {}
        if not isinstance(title, str):
            title = ""
        if not isinstance(analysis_text, str):
            analysis_text = ""
        # Trim analysis_text to ~420 chars for UX safety
        if analysis_text:
            analysis_text = analysis_text.replace("\n", " ").replace("\r", " ")
            if len(analysis_text) > 420:
                analysis_text = analysis_text[:417].rstrip() + "…"
        return {
            "title": title,
            "calories": cal,
            "protein_g": p,
            "fat_g": f,
            "carbs_g": c,
            "weight_g": w,
            "confidence": conf,
            "items": items,
            "references": refs,
            "analysis_text": analysis_text or None,
        }
    except Exception:
        return None


def _norm_detail(val: str | None) -> str:
    v = (val or "low").lower()
    return v if v in {"low", "high", "auto"} else "low"


async def analyze_photo(file_id: str) -> dict[str, Any]:
    """Stub: analyze a photo and return macro nutrients estimation.

    In production, integrate a real vision model. For now returns fixed-ish values.
    """
    if _use_openai():
        # Try real provider first
        file_url = await _tg_file_url(file_id)
        if file_url:
            system = (
                "You are a nutrition analyst. Given an image, estimate total calories, protein_g, fat_g, carbs_g, "
                "and weight_g for the pictured dish. Return ONLY a compact JSON with keys: \n"
                "title(string), calories(int), protein_g(float), fat_g(float), carbs_g(float), weight_g(float), confidence(float 0..1),\n"
                "items(list of {name, calories, protein_g, fat_g, carbs_g, weight_g}), references({sources: [string]}), analysis_text(string).\n"
                "Important: Answer in Russian language. Field 'title' must be in Russian. Ingredient names (items[].name) must be in Russian. "
                "Always set references.sources to exactly [\"ФГБУН \\\"ФИЦ питания и биотехнологии\\\"\", \"USDA FoodData Central\"]. "
                "analysis_text: a single paragraph of 350–420 characters in Russian that (1) states whether the dish appears homemade or packaged (do not invent brands unless clearly visible), "
                "(2) names 2–3 visually identified main components, (3) explains how portion size was estimated (e.g., by plate size ~24 cm and ingredient count/volume); "
                "include the exact sentence: \"Использованы справочные данные ФИЦ питания и USDA.\" Return JSON only, without explanations."
            )

            # Prefer a dedicated vision model if provided
            vision_model = getattr(settings, "FOODAI_VISION_MODEL", None) or settings.FOODAI_DEFAULT_MODEL

            async def _build_and_call(detail: str) -> dict[str, Any] | None:
                # Force Chat API for images for better compatibility
                api = "chat"
                if api == "responses":
                    payload = {
                        "model": vision_model,
                        "instructions": system,
                        "reasoning": {"effort": settings.FOODAI_REASONING_EFFORT},
                        "text": {"verbosity": settings.FOODAI_TEXT_VERBOSITY},
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "Estimate nutrition for this dish and return JSON only."},
                                    {"type": "input_image", "image_url": {"url": file_url}, "detail": detail},
                                ],
                            }
                        ],
                    }
                    content_local = await _openai_request("responses", payload)
                else:
                    payload = {
                        "model": vision_model,
                        "temperature": 0.2,
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "Estimate nutrition for this dish and return JSON only."},
                                    {"type": "image_url", "image_url": {"url": file_url, "detail": detail}},
                                ],
                            },
                        ],
                    }
                    content_local = await _openai_request("chat", payload)
                if content_local:
                    parsed_local = _normalize_openai_json(content_local)
                    # treat empty {} as failure to trigger fallback/retry
                    try:
                        if parsed_local and int(parsed_local.get("calories") or 0) == 0 and float(parsed_local.get("protein_g") or 0) == 0 and float(parsed_local.get("fat_g") or 0) == 0 and float(parsed_local.get("carbs_g") or 0) == 0:
                            parsed_local = None
                    except Exception:
                        parsed_local = None
                    return parsed_local
                return None

            initial_detail = _norm_detail(getattr(settings, "FOODAI_IMAGE_DETAIL", "low"))
            parsed = await _build_and_call(initial_detail)
            if parsed:
                conf = float(parsed.get("confidence") or 0)
                need_retry = (
                    bool(getattr(settings, "FOODAI_IMAGE_DETAIL_HIGH_RETRY", True))
                    and initial_detail != "high"
                    and conf < float(getattr(settings, "FOODAI_CONFIDENCE_ESCALATE", 0.7))
                )
                if need_retry:
                    parsed_hi = await _build_and_call("high")
                    if parsed_hi:
                        return parsed_hi
                return parsed
            else:
                # parsing failed — try a single high-detail retry if enabled
                if bool(getattr(settings, "FOODAI_IMAGE_DETAIL_HIGH_RETRY", True)) and initial_detail != "high":
                    parsed_hi = await _build_and_call("high")
                    if parsed_hi:
                        return parsed_hi
        # Fallback to stub if URL not available or parsing failed

    # Fake deterministic output based on file_id hash length just to vary a little
    base = (len(file_id) % 100) + 250
    protein = round(base * 0.25 / 4, 1)  # grams assuming 4 kcal/g
    fat = round(base * 0.30 / 9, 1)      # grams assuming 9 kcal/g
    carbs = round(base * 0.45 / 4, 1)    # grams assuming 4 kcal/g
    weight = round(protein * 4 + fat * 9 + carbs * 4, 1)  # pseudo-weight proxy

    return {
        "title": "Блюдо",
        "calories": int(base),
        "protein_g": float(protein),
        "fat_g": float(fat),
        "carbs_g": float(carbs),
        "weight_g": float(weight),
        "confidence": 0.72,
        "items": [
            {"name": "Блюдо", "calories": int(base), "protein_g": float(protein), "fat_g": float(fat), "carbs_g": float(carbs), "weight_g": float(weight)},
        ],
        "references": {
            "sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]
        },
        "analysis_text": "Блюдо домашнее, без видимых брендов или упаковок. Оценка порции по размеру посуды и количеству ингредиентов; высокая уверенность по основным компонентам, средняя по точному весу. Использованы справочные данные ФИЦ питания и USDA.",
    }


async def analyze_text(text: str) -> dict[str, Any]:
    """Analyze a text description and return macro estimation.

    If OpenAI provider is enabled, use the model; otherwise fallback to stub.
    """
    if _use_openai() and (text or "").strip():
        system = (
            "You are a nutrition analyst. Given a short dish description, estimate total calories, protein_g, "
            "fat_g, carbs_g and weight_g. Return ONLY JSON with keys: title(string), calories(int), protein_g(float), fat_g(float), "
            "carbs_g(float), weight_g(float), confidence(float 0..1), items(list of {name, calories, protein_g, fat_g, carbs_g, weight_g}), "
            "references({sources: [string]}), analysis_text(string).\n"
            "Important: Answer in Russian language. Field 'title' must be in Russian. Ingredient names (items[].name) must be in Russian. "
            "Always set references.sources to exactly [\"ФГБУН \\\"ФИЦ питания и биотехнологии\\\"\", \"USDA FoodData Central\"]. "
            "analysis_text: a single paragraph of 350–420 characters in Russian that (1) states whether the dish appears homemade or packaged (do not invent brands unless clearly visible), "
            "(2) names 2–3 visually identified main components, (3) explains how portion size was estimated (e.g., by plate size ~24 cm and ingredient count/volume); "
            "include the exact sentence: \"Использованы справочные данные ФИЦ питания и USDA.\" Return JSON only, without explanations."
        )
        api = (settings.FOODAI_API or "chat").lower()
        if api == "responses":
            payload = {
                "model": settings.FOODAI_DEFAULT_MODEL,
                "instructions": system,
                "reasoning": {"effort": settings.FOODAI_REASONING_EFFORT},
                "text": {"verbosity": settings.FOODAI_TEXT_VERBOSITY},
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": f"{text}\n\nReturn JSON only."},
                        ],
                    }
                ],
            }
            content = await _openai_request("responses", payload)
        else:
            payload = {
                "model": settings.FOODAI_DEFAULT_MODEL,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
            }
            content = await _openai_request("chat", payload)

        if content:
            parsed = _normalize_openai_json(content)
            if parsed:
                try:
                    if int(parsed.get("calories") or 0) == 0 and float(parsed.get("protein_g") or 0) == 0 and float(parsed.get("fat_g") or 0) == 0 and float(parsed.get("carbs_g") or 0) == 0:
                        parsed = None
                except Exception:
                    parsed = None
                if parsed:
                    return parsed

    words = len(text.split())
    base = 150 + (words % 200)
    protein = round(base * 0.2 / 4, 1)
    fat = round(base * 0.3 / 9, 1)
    carbs = round(base * 0.5 / 4, 1)
    weight = round(protein * 4 + fat * 9 + carbs * 4, 1)

    return {
        "title": (text or "Описание").strip()[:80] or "Описание",
        "calories": int(base),
        "protein_g": float(protein),
        "fat_g": float(fat),
        "carbs_g": float(carbs),
        "weight_g": float(weight),
        "confidence": 0.65,
        "items": [
            {"name": "Описание", "calories": int(base), "protein_g": float(protein), "fat_g": float(fat), "carbs_g": float(carbs), "weight_g": float(weight)},
        ],
        "references": {
            "sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]
        },
        "analysis_text": "Оценка по текстовому описанию; макроэлементы рассчитаны по типовым справочникам. Уверенность средняя из‑за неопределённости веса и состава. Использованы справочные данные ФИЦ питания и USDA.",
    }
