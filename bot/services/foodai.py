from __future__ import annotations

from typing import Any
import json
import re
import asyncio
from aiohttp import ClientSession
from bot.core.config import settings
from loguru import logger
from bot.metrics import (
    foodai_analysis_text_rewrite,
    foodai_precheck_is_food,
    foodai_precheck_not_food,
    foodai_precheck_error,
    foodai_provider_error,
    foodai_file_url_missing,
    foodai_lexicon_is_food,
)


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
    for _attempt in range(2):
        try:
            async with ClientSession() as sess:
                async with sess.get(f"{api_base}/getFile", params={"file_id": file_id}, timeout=settings.FOODAI_TIMEOUT) as r:
                    data = await r.json()
            if not data.get("ok"):
                raise RuntimeError("tg_api_not_ok")
            file_path = (data.get("result") or {}).get("file_path")
            if not file_path:
                raise RuntimeError("file_path_missing")
            try:
                logger.debug("FoodAI: TG file_path resolved (len={}): {}", len(file_path), file_path)
            except Exception:
                pass
            return f"https://api.telegram.org/file/bot{token}/{file_path}"
        except Exception:
            try:
                await asyncio.sleep(0.2)
            except Exception:
                pass
            continue
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


def _lexicon_is_food_text(text: str | None) -> bool | None:
    """Lightweight lexical whitelist for short texts.

    Returns True if text clearly denotes an edible item or beverage (e.g., "кофе").
    Returns None otherwise (caller proceeds to LLM precheck).
    """
    try:
        t = (text or "").strip().lower()
        if not t:
            return None
        # normalize basic punctuation
        for ch in [",", ".", "!", "?", ":", ";", "(", ")", "[", "]", "{", "}"]:
            t = t.replace(ch, " ")
        t = " ".join(t.split())
        # one-token beverage/food whitelist (Russian forms)
        lex = {
            "кофе",
            "чай",
            "вода",
            "сок",
            "компот",
            "морс",
            "квас",
            "лимонад",
            "молоко",
            "кефир",
            "какао",
            "йогурт",
            "суп",
            "борщ",
            "окрошка",
        }
        # obvious non-food single objects (guard for one-token)
        non_food = {
            "телефон",
            "ноутбук",
            "книга",
            "машина",
            "авто",
            "иконка",
            "эмодзи",
            "смайлик",
        }
        # only trust whitelist on single words to avoid accidental matches
        if " " not in t:
            if t in lex:
                return True
            if t in non_food:
                return None  # explicitly not promoting to True; LLM precheck will handle as not food
        return None
    except Exception:
        return None


def _normalize_openai_json(raw_text: str) -> dict[str, Any] | None:
    """Parse assistant content as JSON and coerce types safely.

    Expected keys: title, calories, protein_g, fat_g, carbs_g, weight_g, confidence, items[], references{sources[]}, analysis_text, appearance, not_food.
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
        not_food = bool(data.get("not_food") or False)
        appearance = data.get("appearance") or {}
        if not isinstance(items, list):
            items = []
        if not isinstance(refs, dict):
            refs = {}
        if not isinstance(title, str):
            title = ""
        if not isinstance(analysis_text, str):
            analysis_text = ""
        if not isinstance(appearance, dict):
            appearance = {}
        # Coerce appearance fields lightly
        try:
            if "plate_diameter_cm" in appearance and appearance["plate_diameter_cm"] is not None:
                appearance["plate_diameter_cm"] = int(float(appearance["plate_diameter_cm"]))
        except Exception:
            appearance["plate_diameter_cm"] = None
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
            "appearance": appearance,
            "not_food": not_food,
        }
    except Exception:
        return None


def _top_components(items: list[dict[str, Any]] | list | None, k: int = 3) -> list[str]:
    names: list[str] = []
    for it in (items or []):
        try:
            n = str((it or {}).get("name") or "").strip()
            if n and n not in names:
                names.append(n)
        except Exception:
            continue
        if len(names) >= k:
            break
    return names


def _needs_rewrite(analysis_text: str | None, items: list | None) -> tuple[bool, str]:
    MUST_PHRASE = "Использованы справочные данные ФИЦ питания и USDA."
    txt = (analysis_text or "").strip()
    if not txt:
        return True, "empty"
    L = len(txt)
    if L < 330 or L > 450:
        return True, "length"
    # cliché in the first sentence ("выгляд"/"похож"): ban in the opener
    try:
        first_sent = re.split(r"[\.!?]", txt, maxsplit=1)[0]
    except Exception:
        first_sent = txt
    if re.search(r"\b(выгляд\w*|похож\w*)\b", first_sent, flags=re.IGNORECASE):
        return True, "cliche"
    # must contain at least 2 different component names
    comps = _top_components(items, 3)
    hit = 0
    for n in comps:
        if n and n.lower() in txt.lower():
            hit += 1
    if hit < 2:
        return True, "components"
    # mandatory citation
    if MUST_PHRASE.lower() not in txt.lower():
        return True, "citation"
    return False, ""


def _sanitize_first_sentence(items: list | None, text: str) -> str:
    """If the first sentence contains banned clichés, replace it with
    'На фото …' + 2–3 components. Keep the rest of the paragraph intact.
    """
    try:
        parts = re.split(r"([\.!?])", text, maxsplit=1)
        first = parts[0]
        # Check cliché in first sentence
        if re.search(r"\b(выгляд\w*|похож\w*)\b", first, flags=re.IGNORECASE):
            comps = _top_components(items, 3)
            if comps:
                if len(comps) == 1:
                    lead = f"На фото {comps[0]}"
                elif len(comps) == 2:
                    lead = f"На фото {comps[0]} и {comps[1]}"
                else:
                    lead = f"На фото {comps[0]}, {comps[1]} и {comps[2]}"
                rest = "" if len(parts) < 3 else (parts[1] + parts[2])
                return (lead + "." + rest).strip()
    except Exception:
        pass
    return text


async def _compose_analysis_text(items: list | None, appearance: dict | None, confidence: float | int | None) -> str | None:
    """Generate a concise analysis paragraph 350–420 chars based on structured inputs.

    Returns None on failure.
    """
    if not _use_openai():
        return None
    comps = _top_components(items, 3)
    MUST_PHRASE = "Использованы справочные данные ФИЦ питания и USDA."
    plate_visible = bool((appearance or {}).get("plate_visible"))
    plate_diam = (appearance or {}).get("plate_diameter_cm")
    is_packaged = bool((appearance or {}).get("is_packaged"))
    method_hint = "по количеству и объёму ингредиентов"
    if plate_visible and plate_diam:
        method_hint = f"по размеру тарелки (~{int(plate_diam)} см) и количеству ингредиентов"
    pkg_hint = "в упаковке" if is_packaged else "без упаковки, на тарелке"
    system = (
        "Ты — ИИ-нутрициолог. Сформулируй один абзац (350–420 символов) на русском, без markdown. "
        "Начни первое предложение со слов: 'На фото ...' и перечисли 2–3 основных компонента дословно из списка. "
        "Опиши вид: " + pkg_hint + ". Объясни, как оценивалась порция (" + method_hint + "). "
        "Обязательно включи точную фразу: \"" + MUST_PHRASE + "\". "
        "Запрещено использовать слова с корнями 'выгляд' и 'похож' в первом предложении. Без брендов, если их нет."
    )
    user = (
        "Компоненты: " + ", ".join(comps) + ". "
        + f"Уверенность: {int(float(confidence or 0)*100)}%. "
        + ("Тарелка видна." if plate_visible else "Тарелка не видна.")
    )
    payload = {
        "model": settings.FOODAI_DEFAULT_MODEL,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        content = await _openai_request("chat", payload)
        if not content:
            return None
        txt = content.strip().replace("\n", " ")
        # enforce phrase and length cap + sanitize opener if needed
        if MUST_PHRASE.lower() not in txt.lower():
            if not txt.endswith("."):
                txt += "."
            txt += " " + MUST_PHRASE
        txt = _sanitize_first_sentence(items, txt)
        if len(txt) > 420:
            txt = txt[:417].rstrip() + "…"
        return txt
    except Exception:
        return None


def _norm_detail(val: str | None) -> str:
    """Normalize image detail value to one of: low|high|auto. Defaults to low."""
    v = (val or "low").lower()
    return v if v in {"low", "high", "auto"} else "low"


async def analyze_photo(file_id: str) -> dict[str, Any]:
    """Stub: analyze a photo and return macro nutrients estimation.

    In production, integrate a real vision model. For now returns fixed-ish values.
    """
    if _use_openai():
        # Try real provider first
        file_url = await _tg_file_url(file_id)
        if not file_url:
            try:
                foodai_file_url_missing.labels(source="photo").inc()
            except Exception:
                pass
            return {"error": "file_url_unavailable"}
        else:
            # Pre-check with a single retry; failures -> provider_error (not not_food)
            is_food = await _foodness_photo(file_url)
            if is_food is None:
                try:
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
                is_food = await _foodness_photo(file_url)
            if is_food is False:
                return {
                    "title": None,
                    "calories": 0,
                    "protein_g": 0.0,
                    "fat_g": 0.0,
                    "carbs_g": 0.0,
                    "weight_g": 0.0,
                    "confidence": 0.0,
                    "items": [],
                    "references": {"sources": [
                        "ФГБУН \"ФИЦ питания и биотехнологии\"",
                        "USDA FoodData Central",
                    ]},
                    "analysis_text": None,
                    "appearance": {},
                    "not_food": True,
                }
            if is_food is None:
                try:
                    foodai_provider_error.labels(source="photo", error="precheck_failed").inc()
                except Exception:
                    pass
                return {"error": "provider_unavailable"}
            system = (
                "You are a nutrition analyst. Given an image, estimate total calories, protein_g, fat_g, carbs_g, "
                "and weight_g for the pictured dish. Return ONLY a compact JSON with keys: \n"
                "title(string), calories(int), protein_g(float), fat_g(float), carbs_g(float), weight_g(float), confidence(float 0..1),\n"
                "items(list of {name, calories, protein_g, fat_g, carbs_g, weight_g, is_liquid:boolean}), references({sources: [string]}), analysis_text(string), appearance({is_packaged: boolean, plate_visible: boolean, plate_diameter_cm: int|null}), not_food(boolean).\n"
                "Important: Answer in Russian language. Field 'title' must be in Russian. Ingredient names (items[].name) must be in Russian. "
                "Always set references.sources to exactly [\"ФГБУН \\\"ФИЦ питания и биотехнологии\\\"\", \"USDA FoodData Central\"]. "
                "If the image clearly does not contain any food or drinks, set not_food=true and keep items minimal. "
                "For liquids, set items[].is_liquid=true (e.g., вода, сок, кофе, чай, молоко, кефир, йогурт питьевой, бульон, суп-пюре, лимонад). "
                "analysis_text: a single paragraph of 350–420 characters in Russian that (1) states whether the dish appears homemade or packaged (do not invent brands unless clearly visible), "
                "(2) names 2–3 visually identified main components, (3) explains how portion size was estimated (e.g., by plate size ~24 cm and ingredient count/volume); "
                "include the exact sentence: \"Использованы справочные данные ФИЦ питания и USDA.\" Return JSON only, without explanations."
            )
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
                # Optional rewrite of analysis_text
                try:
                    mode = (getattr(settings, "FOODAI_ANALYSIS_REWRITE", "auto") or "auto").lower()
                except Exception:
                    mode = "auto"
                do_rewrite = mode == "always"
                reason = ""
                if mode == "auto":
                    do_rewrite, reason = _needs_rewrite(parsed.get("analysis_text"), parsed.get("items"))
                if do_rewrite:
                    try:
                        new_txt = await _compose_analysis_text(parsed.get("items"), parsed.get("appearance") or {}, parsed.get("confidence"))
                        if new_txt:
                            parsed["analysis_text"] = new_txt
                            foodai_analysis_text_rewrite.labels(reason=reason or "auto").inc()
                        else:
                            foodai_analysis_text_rewrite.labels(reason or "error").inc()
                    except Exception:
                        try:
                            foodai_analysis_text_rewrite.labels("error").inc()
                        except Exception:
                            pass
                # Final safety: sanitize opener even if rewrite didn't trigger
                try:
                    if parsed.get("analysis_text"):
                        txt = _sanitize_first_sentence(parsed.get("items"), str(parsed.get("analysis_text") or ""))
                        if txt:
                            # re-cap length after sanitation
                            if len(txt) > 420:
                                txt = txt[:417].rstrip() + "…"
                            parsed["analysis_text"] = txt
                except Exception:
                    pass
                return parsed
            else:
                # parsing failed — try a single high-detail retry if enabled
                if bool(getattr(settings, "FOODAI_IMAGE_DETAIL_HIGH_RETRY", True)) and initial_detail != "high":
                    parsed_hi = await _build_and_call("high")
                    if parsed_hi:
                        return parsed_hi
        # If we are here and parsing still failed — return provider error (no stub)
        try:
            foodai_provider_error.labels(source="photo", error="provider_unavailable").inc()
        except Exception:
            pass
        return {"error": "provider_unavailable"}


async def _foodness_photo(file_url: str) -> bool | None:
    """Return False if image likely does NOT contain food/drink. True if contains. None on failure."""
    if not _use_openai():
        return None
    system = (
        "You are a binary classifier. Determine if the image shows edible food or a drink. "
        "Respond with strict JSON: {\"is_food\": boolean}. "
        "If a cup, glass, mug, bottle, plate or bowl is visible, or a food package/container is visible, set is_food=true. "
        "If unsure or ambiguous, set is_food=false."
    )
    vision_model = getattr(settings, "FOODAI_VISION_MODEL", None) or settings.FOODAI_DEFAULT_MODEL
    payload = {
        "model": vision_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Does this image contain food or drink?"},
                    {"type": "image_url", "image_url": {"url": file_url}},
                ],
            },
        ],
    }
    try:
        content = await _openai_request("chat", payload)
        if not content:
            try:
                foodai_precheck_error.labels(source="photo", reason="http").inc()
            except Exception:
                pass
            return None
        try:
            data = json.loads(content)
        except Exception:
            try:
                data = json.loads((_strip_code_fence(content) or "{}"))
            except Exception:
                try:
                    foodai_precheck_error.labels(source="photo", reason="json").inc()
                except Exception:
                    pass
                return None
        is_food = bool((data or {}).get("is_food"))
        try:
            (foodai_precheck_is_food if is_food else foodai_precheck_not_food).labels(source="photo").inc()
        except Exception:
            pass
        return is_food
    except Exception:
        try:
            foodai_precheck_error.labels(source="photo", reason="other").inc()
        except Exception:
            pass
        return None


async def _foodness_text(text: str) -> bool | None:
    """Return False if text likely does NOT describe food/drink. True if describes. None on failure."""
    if not _use_openai():
        return None
    system = (
        "You are a binary classifier. Determine if the text describes edible food or a drink. "
        "Respond with strict JSON: {\"is_food\": boolean}. If unsure or ambiguous, set is_food=false. "
        "If the text is a single common beverage name in Russian (e.g., кофе, чай, вода, сок, компот, морс, квас, лимонад, молоко, кефир, какао, йогурт), set is_food=true. "
        "If the text is a single common household object (e.g., телефон, ноутбук, книга, машина, иконка, смайлик), set is_food=false."
    )
    primary_model = getattr(settings, "FOODAI_DEFAULT_MODEL", None) or getattr(settings, "FOODAI_VISION_MODEL", None)
    payload = {
        "model": primary_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": (text or "")[:500]},
        ],
    }
    try:
        content = await _openai_request("chat", payload)
        if not content and getattr(settings, "FOODAI_VISION_MODEL", None) and settings.FOODAI_VISION_MODEL != primary_model:
            payload["model"] = settings.FOODAI_VISION_MODEL
            content = await _openai_request("chat", payload)
        if not content:
            try:
                foodai_precheck_error.labels(source="text", reason="http").inc()
            except Exception:
                pass
            return None
        try:
            data = json.loads(content)
        except Exception:
            try:
                data = json.loads((_strip_code_fence(content) or "{}"))
            except Exception:
                try:
                    foodai_precheck_error.labels(source="text", reason="json").inc()
                except Exception:
                    pass
                return None
        is_food = bool((data or {}).get("is_food"))
        try:
            (foodai_precheck_is_food if is_food else foodai_precheck_not_food).labels(source="text").inc()
        except Exception:
            pass
        return is_food
    except Exception:
        try:
            foodai_precheck_error.labels(source="text", reason="other").inc()
        except Exception:
            pass
        return None


async def analyze_text(text: str) -> dict[str, Any]:
    """Analyze a text description and return macro estimation.

    If OpenAI provider is enabled, use the model; otherwise fallback to stub.
    """
    if _use_openai() and (text or "").strip():
        # Lexicon whitelist for short beverage/food names
        lex_hit = _lexicon_is_food_text(text)
        if lex_hit is True:
            try:
                foodai_lexicon_is_food.labels(source="text").inc()
            except Exception:
                pass
            is_food = True
        else:
            # Strict pre-check; any failure counts as not_food (conservative)
            is_food = await _foodness_text(text)
        if is_food is False or is_food is None:
            return {
                "title": None,
                "calories": 0,
                "protein_g": 0.0,
                "fat_g": 0.0,
                "carbs_g": 0.0,
                "weight_g": 0.0,
                "confidence": 0.0,
                "items": [],
                "references": {"sources": [
                    "ФГБУН \"ФИЦ питания и биотехнологии\"",
                    "USDA FoodData Central",
                ]},
                "analysis_text": None,
                "appearance": {},
                "not_food": True,
            }
        system = (
            "You are a nutrition analyst. Given a short dish description, estimate total calories, protein_g, "
            "fat_g, carbs_g and weight_g. Return ONLY JSON with keys: title(string), calories(int), protein_g(float), fat_g(float), "
            "carbs_g(float), weight_g(float), confidence(float 0..1), items(list of {name, calories, protein_g, fat_g, carbs_g, weight_g, is_liquid:boolean}), "
            "references({sources: [string]}), analysis_text(string), appearance({is_packaged: boolean, plate_visible: boolean, plate_diameter_cm: int|null}), not_food(boolean).\n"
            "Important: Answer in Russian language. Field 'title' must be in Russian. Ingredient names (items[].name) must be in Russian. "
            "Always set references.sources to exactly [\"ФГБУН \\\"ФИЦ питания и биотехнологии\\\"\", \"USDA FoodData Central\"]. "
            "If the description clearly does not refer to food or drinks, set not_food=true and keep items minimal. "
            "For liquids, set items[].is_liquid=true. "
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
                    # Optional rewrite
                    try:
                        mode = (getattr(settings, "FOODAI_ANALYSIS_REWRITE", "auto") or "auto").lower()
                    except Exception:
                        mode = "auto"
                    do_rewrite = mode == "always"
                    reason = ""
                    if mode == "auto":
                        do_rewrite, reason = _needs_rewrite(parsed.get("analysis_text"), parsed.get("items"))
                    if do_rewrite:
                        try:
                            new_txt = await _compose_analysis_text(parsed.get("items"), parsed.get("appearance") or {}, parsed.get("confidence"))
                            if new_txt:
                                parsed["analysis_text"] = new_txt
                                foodai_analysis_text_rewrite.labels(reason=reason or "auto").inc()
                            else:
                                foodai_analysis_text_rewrite.labels(reason or "error").inc()
                        except Exception:
                            try:
                                foodai_analysis_text_rewrite.labels("error").inc()
                            except Exception:
                                pass
                    # Final safety: sanitize opener even if rewrite didn't trigger
                    try:
                        if parsed.get("analysis_text"):
                            txt = _sanitize_first_sentence(parsed.get("items"), str(parsed.get("analysis_text") or ""))
                            if txt:
                                if len(txt) > 420:
                                    txt = txt[:417].rstrip() + "…"
                                parsed["analysis_text"] = txt
                    except Exception:
                        pass
                    return parsed
    try:
        foodai_provider_error.labels(source="text", error="provider_unavailable").inc()
    except Exception:
        pass
    return {"error": "provider_unavailable"}


# ===== Edit flow support =====
async def refine_meal(base: dict[str, Any], instruction: str) -> dict[str, Any]:
    """Apply a text edit instruction to an existing meal.

    Supports actions:
      - add: "добавить/добавь/прибавить/+ <ингредиент> <N> [г|гр|грамм|мл|л|l|шт]"
      - remove: "убрать/удалить/без/минус/- <ингредиент>"
      - replace: "заменить/замени/поменять <from> на <to> [N ед]"
      - scale portion: "увеличить/уменьшить порцию на <N>%" | "в <K> раза" | "x<K>" | "+N%" | "-N%"
      - change qty: "<ингредиент> +N г" | "увеличить/уменьшить <ингредиент> до N г"

    Returns meal dict compatible with analyze_* outputs plus meta {action, reason?, delta_*}.
    """
    try:
        title = str((base or {}).get("title") or "").strip()
        items_in = list((base or {}).get("items") or [])
        weight_g = float((base or {}).get("weight_g") or 0)
        base_cal = int((base or {}).get("calories") or 0)
        base_p = float((base or {}).get("protein_g") or 0)
        base_f = float((base or {}).get("fat_g") or 0)
        base_c = float((base or {}).get("carbs_g") or 0)
    except Exception:
        title, items_in, weight_g = "", [], 0.0
        base_cal = base_p = base_f = base_c = 0

    instr = (instruction or "").strip().lower()
    if not instr:
        return {"error": "empty_instruction", "meta": {"action": "unknown", "reason": "parse"}}

    # --- helpers ---
    densities = {
        "вода": 1.0,
        "молоко": 1.03,
        "кефир": 1.03,
        "йогурт": 1.03,
        "бульон": 1.0,
        "суп": 1.0,
        "масло": 0.91,
        "лимонад": 1.02,
        "сок": 1.04,
    }
    pcs = {  # rough grams per piece
        "яйцо": 50,
        "ломтик хлеба": 30,
        "сырок": 40,
        "яблоко": 180,
    }

    def _clone_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for it in (items or []):
            try:
                out.append({
                    "name": str((it or {}).get("name") or "Ингредиент"),
                    "weight_g": float((it or {}).get("weight_g") or 0) if (it or {}).get("weight_g") is not None else None,
                    "calories": float((it or {}).get("calories") or 0) if (it or {}).get("calories") is not None else None,
                    "protein_g": float((it or {}).get("protein_g") or 0) if (it or {}).get("protein_g") is not None else None,
                    "fat_g": float((it or {}).get("fat_g") or 0) if (it or {}).get("fat_g") is not None else None,
                    "carbs_g": float((it or {}).get("carbs_g") or 0) if (it or {}).get("carbs_g") is not None else None,
                })
            except Exception:
                continue
        return out

    def _sum_items(items: list[dict[str, Any]]) -> tuple[int, float, float, float, float]:
        cal = 0
        p = f = c = 0.0
        total_w = 0.0
        for it in items:
            try:
                if it.get("calories") is not None:
                    cal += int(float(it.get("calories") or 0))
                if it.get("protein_g") is not None:
                    p += float(it.get("protein_g") or 0)
                if it.get("fat_g") is not None:
                    f += float(it.get("fat_g") or 0)
                if it.get("carbs_g") is not None:
                    c += float(it.get("carbs_g") or 0)
                if it.get("weight_g") is not None:
                    total_w += float(it.get("weight_g") or 0)
            except Exception:
                continue
        return int(cal), round(p, 1), round(f, 1), round(c, 1), total_w

    def _qty_to_grams(name: str, qty: float, unit: str | None) -> tuple[float, bool]:
        unit_l = (unit or "г").lower()
        if unit_l in {"г", "гр", "грамм", "gram", "g"}:
            return float(qty), False
        if unit_l in {"кг", "kg"}:
            return float(qty) * 1000.0, False
        if unit_l in {"мл", "ml"}:
            # convert to g using density
            d = 1.0
            key = (name or "").lower()
            for k, val in densities.items():
                if k in key:
                    d = val
                    break
            return float(qty) * d, True
        if unit_l in {"л", "l"}:
            # liters -> ml -> g
            ml = float(qty) * 1000.0
            return _qty_to_grams(name, ml, "мл")
        if unit_l in {"шт", "pc", "pcs"}:
            key = (name or "").lower()
            for k, val in pcs.items():
                if k in key:
                    return float(qty) * float(val), False
            # unknown piece -> fallback 100g
            return float(qty) * 100.0, False
        # default
        return float(qty), False

    def _estimate_from_name(name: str, qty_g: float) -> tuple[int, float, float, float]:
        per100 = {
            "сыр": {"cal": 330, "p": 25.0, "f": 26.0, "c": 1.3},
            "кетчуп": {"cal": 100, "p": 1.5, "f": 0.2, "c": 24.0},
            "масло": {"cal": 900, "p": 0.0, "f": 100.0, "c": 0.0},
            "оливковое масло": {"cal": 900, "p": 0.0, "f": 100.0, "c": 0.0},
            "майонез": {"cal": 680, "p": 1.0, "f": 75.0, "c": 3.0},
            "сметана": {"cal": 206, "p": 2.8, "f": 20.0, "c": 3.2},
            "соус": {"cal": 150, "p": 1.0, "f": 5.0, "c": 24.0},
        }
        n = (name or "").lower()
        hit = None
        for k in per100.keys():
            if k in n:
                hit = per100[k]
                break
        if not hit:
            hit = {"cal": 250, "p": 8.0, "f": 18.0, "c": 12.0}
        factor = max(qty_g, 0.0) / 100.0
        cal = int(round(hit["cal"] * factor))
        p = round(hit["p"] * factor, 1)
        f = round(hit["f"] * factor, 1)
        c = round(hit["c"] * factor, 1)
        return cal, p, f, c

    def _find_indices(items: list[dict[str, Any]], needle: str) -> list[int]:
        res: list[int] = []
        n = (needle or "").lower().strip()
        for i, it in enumerate(items):
            try:
                if n and n in str(it.get("name") or "").lower():
                    res.append(i)
            except Exception:
                continue
        return res

    # --- parse order: replace > change_qty > add > remove > scale ---
    action = "unknown"
    reason = None

    # replace
    m = re.search(r"(?:заменить|замени|поменять)\s+([a-zа-яё\-\s]+?)\s+на\s+([a-zа-яё\-\s]+?)(?:\s+(\d{1,4})\s*(г|гр|грамм|мл|ml|л|l|шт))?\b", instr, flags=re.IGNORECASE)
    if m:
        action = "replace"
        old_name = (m.group(1) or "").strip()
        new_name = (m.group(2) or "").strip()
        qty = float(m.group(3)) if m.group(3) else None
        unit = (m.group(4) or None)
        items = _clone_items(items_in)
        idxs = _find_indices(items, old_name)
        if len(idxs) == 0:
            return {"error": "not_found", "meta": {"action": action, "reason": "not_found"}}
        if len(idxs) > 1:
            return {"error": "ambiguous", "meta": {"action": action, "reason": "ambiguous"}}
        idx = idxs[0]
        # remove old
        try:
            old_item = items.pop(idx)
        except Exception:
            old_item = None
        # decide new qty
        new_qty_g: float
        if qty is not None:
            new_qty_g, _ = _qty_to_grams(new_name, qty, unit)
        else:
            try:
                new_qty_g = float((old_item or {}).get("weight_g") or 100.0)
            except Exception:
                new_qty_g = 100.0
        # caps
        new_qty_g = max(1.0, min(1000.0, new_qty_g))
        cal, p, f, c = _estimate_from_name(new_name, new_qty_g)
        items.append({
            "name": new_name,
            "weight_g": new_qty_g,
            "calories": cal,
            "protein_g": p,
            "fat_g": f,
            "carbs_g": c,
        })
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        # title replacement if contains old token
        new_title = title or "Блюдо"
        try:
            if old_name and old_name.lower() in new_title.lower():
                new_title = re.sub(old_name, new_name, new_title, flags=re.IGNORECASE)
            elif new_name and (" с " + new_name.lower()) not in new_title.lower():
                new_title = f"{new_title} с {new_name}"
        except Exception:
            pass
        return {
            "title": new_title,
            "calories": int(out_cal),
            "protein_g": float(out_p),
            "fat_g": float(out_f),
            "carbs_g": float(out_c),
            "weight_g": float(out_w if out_w > 0 else weight_g),
            "confidence": float((base or {}).get("confidence") or 0.8),
            "items": items,
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": None,
            "appearance": {},
            "not_food": False,
            "meta": {"action": action, "delta_cal": int(out_cal - base_cal)},
        }

    # change quantity to N g/ml
    m = re.search(r"(?:увеличить|уменьшить|сделать|до)\s+([a-zа-яё\-\s]+?)\s*(?:до)?\s*(\+?\-?\d{1,4})\s*(г|гр|грамм|мл|ml|л|l)\b", instr, flags=re.IGNORECASE)
    if not m:
        # pattern: '<name> +N г' but avoid leading action verbs (добавить/убрать/заменить)
        if re.match(r"\s*(?:добавить|добавь|положить|прибавить|убрать|удалить|без|минус|\-|заменить|замени|поменять)\b", instr, flags=re.IGNORECASE):
            m = None
        else:
            m = re.search(r"^([a-zа-яё\-\s]+?)\s*([\+\-]?\d{1,4})\s*(г|гр|грамм|мл|ml|л|l)\b", instr, flags=re.IGNORECASE)
    if m:
        action = "change_qty"
        name = (m.group(1) or "").strip()
        qty = float(m.group(2))
        unit = (m.group(3) or None)
        items = _clone_items(items_in)
        idxs = _find_indices(items, name)
        if len(idxs) == 0:
            return {"error": "not_found", "meta": {"action": action, "reason": "not_found"}}
        if len(idxs) > 1:
            return {"error": "ambiguous", "meta": {"action": action, "reason": "ambiguous"}}
        idx = idxs[0]
        new_qty_g, _ = _qty_to_grams(name, qty, unit)
        new_qty_g = max(1.0, min(1000.0, new_qty_g))
        cal, p, f, c = _estimate_from_name(name, new_qty_g)
        items[idx].update({
            "weight_g": new_qty_g,
            "calories": cal,
            "protein_g": p,
            "fat_g": f,
            "carbs_g": c,
        })
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        return {
            "title": title or "Блюдо",
            "calories": int(out_cal),
            "protein_g": float(out_p),
            "fat_g": float(out_f),
            "carbs_g": float(out_c),
            "weight_g": float(out_w if out_w > 0 else weight_g),
            "confidence": float((base or {}).get("confidence") or 0.8),
            "items": items,
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": None,
            "appearance": {},
            "not_food": False,
            "meta": {"action": action, "delta_cal": int(out_cal - base_cal)},
        }

    # add
    m = re.search(r"(?:добавить|добавь|положить|прибавить|\+)?\s*([a-zа-яё\-\s]+?)\s*(\d{1,4})\s*(г|гр|грамм|мл|ml|л|l|шт)?\b", instr, flags=re.IGNORECASE)
    if m:
        action = "add"
        add_name = (m.group(1) or "").strip().strip('- ')
        add_name = re.sub(r"^(?:добавить|добавь|положить|прибавить)\s+", "", add_name, flags=re.IGNORECASE)
        add_qty = float(m.group(2))
        unit = (m.group(3) or None)
        qty_g, _is_liq = _qty_to_grams(add_name, add_qty, unit)
        qty_g = max(1.0, min(1000.0, qty_g))
        add_cal, add_p, add_f, add_c = _estimate_from_name(add_name, qty_g)
        items = _clone_items(items_in)
        items.append({
            "name": add_name,
            "weight_g": qty_g,
            "calories": add_cal,
            "protein_g": add_p,
            "fat_g": add_f,
            "carbs_g": add_c,
        })
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        # Prefer adding estimated delta to base calories to avoid undercount when base items were incomplete
        new_cal = int(max(out_cal, base_cal + add_cal))
        # Title tweak: append 'с <name>' if absent
        new_title = title or "Блюдо"
        try:
            n_clean = add_name.strip()
            if n_clean and (" с " + n_clean.lower()) not in (new_title.lower()):
                new_title = f"{new_title} с {n_clean}"
        except Exception:
            pass
        return {
            "title": new_title,
            "calories": new_cal,
            "protein_g": float(out_p),
            "fat_g": float(out_f),
            "carbs_g": float(out_c),
            "weight_g": float(out_w if out_w > 0 else weight_g + qty_g),
            "confidence": float((base or {}).get("confidence") or 0.8),
            "items": items,
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": None,
            "appearance": {},
            "not_food": False,
            "meta": {"action": action, "delta_cal": int(new_cal - base_cal)},
        }

    # remove
    m = re.search(r"(?:убрать|удалить|без|минус|\-)\s+([a-zа-яё\-\s]+)\b", instr, flags=re.IGNORECASE)
    if m:
        action = "remove"
        name = (m.group(1) or "").strip()
        items = _clone_items(items_in)
        idxs = _find_indices(items, name)
        if len(idxs) == 0:
            return {"error": "not_found", "meta": {"action": action, "reason": "not_found"}}
        if len(idxs) > 1:
            return {"error": "ambiguous", "meta": {"action": action, "reason": "ambiguous"}}
        try:
            items.pop(idxs[0])
        except Exception:
            return {"error": "other", "meta": {"action": action, "reason": "other"}}
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        return {
            "title": title or "Блюдо",
            "calories": int(out_cal if out_cal > 0 else max(0, base_cal - 50)),
            "protein_g": float(out_p),
            "fat_g": float(out_f),
            "carbs_g": float(out_c),
            "weight_g": float(out_w if out_w > 0 else max(0.0, weight_g - 50.0)),
            "confidence": float((base or {}).get("confidence") or 0.8),
            "items": items,
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": None,
            "appearance": {},
            "not_food": False,
            "meta": {"action": action, "delta_cal": int(out_cal - base_cal)},
        }

    # scale portion
    # patterns: 'увеличить/уменьшить порцию на N%', '+N%', '-N%', 'в K раза', 'xK'
    def _parse_scale(t: str) -> float | None:
        m1 = re.search(r"(увеличить|уменьшить)\s+порцию\s+на\s+(\d{1,3})%", t)
        if m1:
            sign = 1 if m1.group(1).lower().startswith("увел") else -1
            pct = float(m1.group(2))
            return max(0.25, min(3.0, 1.0 + sign * pct / 100.0))
        m2 = re.search(r"([\+\-])(\d{1,3})%", t)
        if m2:
            sign = 1 if m2.group(1) == "+" else -1
            pct = float(m2.group(2))
            return max(0.25, min(3.0, 1.0 + sign * pct / 100.0))
        m3 = re.search(r"в\s+(\d+(?:[\.,]\d+)?)\s*раза?", t)
        if m3:
            k = float(m3.group(1).replace(",", "."))
            return max(0.25, min(3.0, k))
        m4 = re.search(r"x\s*(\d+(?:[\.,]\d+)?)", t)
        if m4:
            k = float(m4.group(1).replace(",", "."))
            return max(0.25, min(3.0, k))
        return None

    factor = _parse_scale(instr)
    if factor is not None:
        action = "scale"
        items = _clone_items(items_in)
        for it in items:
            try:
                if it.get("weight_g") is not None:
                    it["weight_g"] = round(float(it.get("weight_g") or 0) * factor, 1)
                # re-estimate macros roughly by name
                name = str(it.get("name") or "")
                w = float(it.get("weight_g") or 0)
                cal, p, f, c = _estimate_from_name(name, w)
                it.update({"calories": cal, "protein_g": p, "fat_g": f, "carbs_g": c})
            except Exception:
                continue
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        # Compute scaled weight using base total weight if available; items list may be partial
        try:
            wg_base = float(weight_g or 0)
        except Exception:
            wg_base = 0.0
        wg_scaled = round((wg_base if wg_base > 0 else out_w) * factor, 1)
        wg_out = round(max(out_w, wg_scaled), 1) if (wg_base or out_w) else 0.0
        return {
            "title": title or "Блюдо",
            "calories": int(out_cal if out_cal > 0 else int(base_cal * factor)),
            "protein_g": float(out_p if out_p > 0 else round(base_p * factor, 1)),
            "fat_g": float(out_f if out_f > 0 else round(base_f * factor, 1)),
            "carbs_g": float(out_c if out_c > 0 else round(base_c * factor, 1)),
            "weight_g": float(wg_out),
            "confidence": float((base or {}).get("confidence") or 0.8),
            "items": items,
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": None,
            "appearance": {},
            "not_food": False,
            "meta": {"action": action, "delta_cal": int((out_cal if out_cal > 0 else int(base_cal * factor)) - base_cal)},
        }

    # Fallback: unsupported
    return {"error": "unsupported_instruction", "meta": {"action": action, "reason": "unsupported"}}
