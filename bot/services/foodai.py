from __future__ import annotations

from typing import Any
import json
import re
import asyncio
import time
from aiohttp import ClientSession
try:
    from bot.services.foodai_edit_llm import interpret_edit
except Exception:  # pragma: no cover
    interpret_edit = None  # type: ignore
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
    foodai_escalation_attempts,
    foodai_escalation_success,
    foodai_escalation_failed,
    foodai_escalation_attempt_dur_ms,
    foodai_escalation_total_dur_ms,
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
    t0 = time.time()
    model = str(payload.get("model") or "")
    try:
        async with ClientSession() as sess:
            async with sess.post(url, headers=headers, data=json.dumps(payload), timeout=settings.FOODAI_TIMEOUT) as r:
                if r.status >= 400:
                    body = await r.text()
                    try:
                        logger.error("OpenAI error | kind={} | endpoint={} | model={} | status={} | body={}",
                                     kind, endpoint, model, r.status, (body or "")[:512])
                    except Exception:
                        pass
                    return None
                data = await r.json()
    except Exception as e:
        dt = int((time.time() - t0) * 1000)
        try:
            logger.exception("OpenAI exception | kind={} | endpoint={} | model={} | dur_ms={} | err={}",
                             kind, endpoint, model, dt, repr(e))
        except Exception:
            pass
        return None

    # Extract assistant text
    try:
        if endpoint == "/chat/completions":
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            return _strip_code_fence(content) if isinstance(content, str) else None
        # Responses API — prefer structured JSON if present
        outputs = data.get("output") or []
        for piece in outputs:
            if (piece or {}).get("type") == "message" and (piece or {}).get("role") == "assistant":
                for c in (piece.get("content") or []):
                    ttype = (c or {}).get("type") or ""
                    if ttype in {"output_json", "json"}:
                        try:
                            obj = c.get("json")
                            if obj is not None:
                                return json.dumps(obj, ensure_ascii=False)
                        except Exception:
                            continue
        if isinstance(data.get("output_text"), str) and data["output_text"].strip():
            return _strip_code_fence(data["output_text"])  # combined text
        out: list[str] = []
        for piece in outputs:
            # Only collect assistant messages to avoid echoing user input
            if (piece or {}).get("type") == "message" and (piece or {}).get("role") == "assistant":
                for c in (piece.get("content") or []):
                    ttype = (c or {}).get("type")
                    # Exclude input_text; keep only model outputs
                    if ttype in {"output_text", "text"}:
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
            # напитки
            "кофе", "чай", "вода", "сок", "компот", "морс", "квас", "лимонад", "молоко", "кефир", "какао", "йогурт",
            # супы
            "суп", "борщ", "окрошка",
            # базовые продукты и блюда (одно слово)
            "рыба", "курица", "индейка", "утка", "говядина", "свинина", "телятина",
            "рис", "гречка", "макароны", "паста", "картофель", "картошка", "овсянка", "каша", "манка",
            "яблоко", "банан", "хлеб", "сыр", "творог", "яйцо", "яйца", "омлет",
            "майонез", "кетчуп", "соус", "сметана", "масло",
            "огурец", "помидор", "томат", "перец", "соль",
            # зелень/травы
            "петрушка", "укроп", "базилик", "кинза", "руккола", "шпинат", "зелень",
            # овощи/грибы/бобовые
            "брокколи", "цветная капуста", "капуста", "кабачок", "баклажан", "свекла", "свёкла", "горошек", "зелёный горошек",
            "шампиньоны", "шампиньон", "вешенки", "вешенка", "фасоль", "нут", "чечевица",
            # фрукты/ягоды
            "клубника", "малина", "черника", "виноград", "апельсин", "груша", "киви", "персик",
            # рыба/морепродукты
            "треска", "хек", "креветки", "креветка", "кальмар",
            # молочное/сыры/масла
            "моцарелла", "творожный сыр", "крем-чиз", "сливочное масло",
            # соусы/добавки/маслины
            "соевый соус", "горчица", "томатная паста", "оливки", "маслины", "песто", "барбекю", "терияки", "цезарь",
            # крупы
            "киноа",
            # хлебцы/выпечка/колбасы/крабовые
            "тортилья", "хлебцы", "круассан", "блин", "олад", "салями", "колбаса", "крабовые палочки",
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
        "Начни первое предложение со слов: 'На фото …' и перечисли 2–3 основных компонента дословно из списка. "
        "Опиши вид: " + pkg_hint + ". Объясни, как оценивалась порция (" + method_hint + "). "
        "Обязательно включи точную фразу: \"" + MUST_PHRASE + "\". "
        "Запрещено использовать слова с корнями 'выгляд' и 'похож' в первом предложении. Без брендов, если их нет. "
        "Не упоминай уровень уверенности."
    )
    user = (
        "Компоненты: " + ", ".join(comps) + ". "
        + ("Тарелка видна." if plate_visible else "Тарелка не видна.")
    )
    # Use a fast/stable model for rewrite to avoid latency/cost spikes
    # Use GPT-5-mini via Responses for rewrite (fast/stable)
    rewrite_model = "gpt-5-mini"
    payload = {
        "model": rewrite_model,
        "instructions": system,
        "reasoning": {"effort": settings.FOODAI_REASONING_EFFORT},
        "text": {"verbosity": settings.FOODAI_TEXT_VERBOSITY},
        "max_output_tokens": 500,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user},
                ],
            }
        ],
    }
    try:
        # Enforce a local, smaller timeout budget for rewrite step
        rw_timeout = int(getattr(settings, "FOODAI_ANALYSIS_REWRITE_TIMEOUT", 8) or 8)
        content = await asyncio.wait_for(_openai_request("responses", payload), timeout=rw_timeout)
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
                # Precheck failed (HTTP/JSON/Other). By default, do NOT abort; proceed with analysis.
                try:
                    foodai_precheck_error.labels(source="photo", reason="precheck_failed").inc()
                except Exception:
                    pass
                try:
                    strict = bool(getattr(settings, "FOODAI_PRECHECK_STRICT", False))
                except Exception:
                    strict = False
                if strict:
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
                "Atomic items only: do not return umbrella items (e.g., 'скрэмбл 160 г'); list base components (яйца, креветки, масло/сливки, хлеб, соусы) as separate entries. "
                "Balance requirements: ensure sum of items.weight_g equals weight_g within ±5% and sum of items.calories equals calories within ±2%. If mismatch occurs, adjust low-impact items first (соусы, листовые). "
                "analysis_text: a single paragraph of 350–420 characters in Russian that (1) states whether the dish appears homemade or packaged (do not invent brands unless clearly visible), "
                "(2) names 2–3 visually identified main components, (3) explains how portion size was estimated (e.g., by plate size ~24 cm and ingredient count/volume); "
                "include the exact sentence: \"Использованы справочные данные ФИЦ питания и USDA.\" Return JSON only, without explanations."
            )

            async def _build_and_call(detail: str, model_id: str) -> dict[str, Any] | None:
                is_gpt5 = str(model_id).startswith("gpt-5")
                use_responses = False
                try:
                    use_responses = bool(getattr(settings, "FOODAI_USE_RESPONSES_FOR_5", False)) and is_gpt5
                except Exception:
                    use_responses = is_gpt5 and False

                # Helper: Chat payload call
                async def _call_chat() -> str | None:
                    payload_chat = {
                        "model": model_id,
                        "temperature": 0,
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "Estimate nutrition for this dish and return json only."},
                                    {"type": "image_url", "image_url": {"url": file_url, "detail": detail}},
                                ],
                            },
                        ],
                    }
                    return await _openai_request("chat", payload_chat)

                # Helper: Responses payload call
                async def _call_responses() -> str | None:
                    payload_resp = {
                        "model": model_id,
                        "instructions": system,
                        "reasoning": {"effort": settings.FOODAI_REASONING_EFFORT},
                        "text": {
                            "verbosity": settings.FOODAI_TEXT_VERBOSITY,
                            "format": {
                                "type": "json_schema",
                                "name": "foodai_result",
                                "strict": True,
                                "schema": {
                                        "type": "object",
                                        "properties": {
                                            "title": {"type": ["string", "null"]},
                                            "calories": {"type": "integer", "minimum": 0},
                                            "protein_g": {"type": "number", "minimum": 0},
                                            "fat_g": {"type": "number", "minimum": 0},
                                            "carbs_g": {"type": "number", "minimum": 0},
                                            "weight_g": {"type": "number", "minimum": 0},
                                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                            "items": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "name": {"type": "string"},
                                                        "calories": {"type": "integer", "minimum": 0},
                                                        "protein_g": {"type": "number", "minimum": 0},
                                                        "fat_g": {"type": "number", "minimum": 0},
                                                        "carbs_g": {"type": "number", "minimum": 0},
                                                        "weight_g": {"type": "number", "minimum": 0},
                                                        "is_liquid": {"type": "boolean"}
                                                    },
                                                    "required": [
                                                        "name",
                                                        "calories",
                                                        "protein_g",
                                                        "fat_g",
                                                        "carbs_g",
                                                        "weight_g",
                                                        "is_liquid"
                                                    ],
                                                    "additionalProperties": False
                                                }
                                            },
                                            "references": {
                                                "type": "object",
                                                "properties": {
                                                    "sources": {"type": "array", "items": {"type": "string"}, "minItems": 1}
                                                },
                                                "required": ["sources"],
                                                "additionalProperties": False
                                            },
                                            "analysis_text": {"type": ["string", "null"]},
                                            "appearance": {
                                                "type": "object",
                                                "properties": {
                                                    "is_packaged": {"type": "boolean"},
                                                    "plate_visible": {"type": "boolean"},
                                                    "plate_diameter_cm": {"type": ["integer", "null"], "minimum": 0}
                                                },
                                                "required": ["is_packaged", "plate_visible", "plate_diameter_cm"],
                                                "additionalProperties": False
                                            },
                                            "not_food": {"type": "boolean"}
                                        },
                                        "required": [
                                            "title",
                                            "calories",
                                            "protein_g",
                                            "fat_g",
                                            "carbs_g",
                                            "weight_g",
                                            "confidence",
                                            "items",
                                            "references",
                                            "analysis_text",
                                            "appearance",
                                            "not_food"
                                        ],
                                        "additionalProperties": False
                                    }
                            }
                        },
                        "max_output_tokens": 1600,
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "Estimate nutrition for this dish and return json only."},
                                    {"type": "input_image", "image_url": file_url, "detail": detail},
                                ],
                            }
                        ],
                    }
                    return await _openai_request("responses", payload_resp)

                content_local: str | None = None
                if use_responses:
                    # Use Responses API (no Chat fallback)
                    content_local = await _call_responses()
                else:
                    # Use Chat API when Responses disabled for this model
                    content_local = await _call_chat()

                if content_local:
                    parsed_local = _normalize_openai_json(content_local)
                    # treat empty {} as failure to trigger fallback/retry
                    try:
                        if parsed_local and int(parsed_local.get("calories") or 0) == 0 and float(parsed_local.get("protein_g") or 0) == 0 and float(parsed_local.get("fat_g") or 0) == 0 and float(parsed_local.get("carbs_g") or 0) == 0:
                            parsed_local = None
                    except Exception:
                        parsed_local = None
                    # Chat fallback if Responses failed to produce parsable/non-zero content
                    if parsed_local is None:
                        try:
                            chat_enabled = bool(getattr(settings, "FOODAI_TEXT_FALLBACK_TO_CHAT", False))
                        except Exception:
                            chat_enabled = False
                        if use_responses and chat_enabled:
                            try:
                                chat_content = await _call_chat()
                            except Exception:
                                chat_content = None
                            if chat_content:
                                parsed_local = _normalize_openai_json(chat_content)
                    return parsed_local
                return None

            # Escalation chain (env-driven) — try models and detail levels in order
            try:
                escalation_enabled = bool(getattr(settings, "FOODAI_VISION_ESCALATION_ENABLED", True))
            except Exception:
                escalation_enabled = True
            if escalation_enabled:
                _chain_t0 = time.perf_counter()
                try:
                    chain_raw = str(getattr(settings, "FOODAI_VISION_ESCALATION_CHAIN", "") or "").strip()
                    model_chain = [m.strip() for m in chain_raw.split(">") if m and m.strip()]
                except Exception:
                    model_chain = []
                if not model_chain:
                    model_chain = [vision_model]
                # optional fallback to 4o-mini if 5-series unavailable
                try:
                    if bool(getattr(settings, "FOODAI_ALLOW_FALLBACK_TO_4O_MINI", True)) and "gpt-4o-mini" not in model_chain:
                        model_chain.append("gpt-4o-mini")
                except Exception:
                    pass
                try:
                    order = str(getattr(settings, "FOODAI_VISION_DETAIL_ORDER", "low>high") or "low>high").split(">")
                    detail_seq = [d.strip().lower() for d in order if d and d.strip().lower() in {"low", "high", "auto"}] or ["low", "high"]
                except Exception:
                    detail_seq = ["low", "high"]

                def _is_weak(res: dict[str, Any]) -> bool:
                    try:
                        conf_th = float(getattr(settings, "FOODAI_ESCALATE_CONF", getattr(settings, "FOODAI_CONFIDENCE_ESCALATE", 0.7)))
                    except Exception:
                        conf_th = 0.7
                    try:
                        conf = float(res.get("confidence") or 0)
                    except Exception:
                        conf = 0.0
                    zero_guard = bool(getattr(settings, "FOODAI_ESCALATE_ZERO_FIELDS", True))
                    items_min = int(getattr(settings, "FOODAI_ESCALATE_ITEMS_MIN", 3) or 3)
                    try:
                        has_zero = int(res.get("calories") or 0) == 0 or float(res.get("protein_g") or 0) == 0 or float(res.get("fat_g") or 0) == 0 or float(res.get("carbs_g") or 0) == 0 or float(res.get("weight_g") or 0) == 0
                    except Exception:
                        has_zero = False
                    try:
                        many_items = len(list(res.get("items") or [])) >= items_min
                    except Exception:
                        many_items = False
                    if conf < conf_th:
                        return True
                    if zero_guard and has_zero:
                        return True
                    if many_items and conf < max(0.75, conf_th):
                        return True
                    return False

                steps = 0
                max_steps = int(getattr(settings, "FOODAI_VISION_MAX_STEPS", 2) or 2)
                best: dict[str, Any] | None = None
                detail_seq_norm = [
                    _norm_detail(d) for d in detail_seq
                ]
                for midx, mid in enumerate(model_chain):
                    for det in detail_seq:
                        steps += 1
                        _attempt_t0 = time.perf_counter()
                        parsed_try = await _build_and_call(_norm_detail(det), mid)
                        # attempt metrics
                        try:
                            foodai_escalation_attempts.labels(
                                from_model=(model_chain[midx - 1] if midx > 0 else "start"),
                                to_model=mid,
                                reason="attempt",
                            ).inc()
                        except Exception:
                            pass
                        try:
                            foodai_escalation_attempt_dur_ms.labels(model=mid, detail=_norm_detail(det)).observe(
                                (time.perf_counter() - _attempt_t0) * 1000.0
                            )
                        except Exception:
                            pass
                        if parsed_try:
                            best = parsed_try
                            last_step = (midx == len(model_chain) - 1 and det == detail_seq[-1])
                            should_stop = (not _is_weak(parsed_try)) or last_step or (steps >= max_steps)
                            if should_stop:
                                # Optional rewrite + sanitize
                                try:
                                    mode = (getattr(settings, "FOODAI_ANALYSIS_REWRITE", "auto") or "auto").lower()
                                except Exception:
                                    mode = "auto"
                                do_rewrite = mode == "always"
                                reason = ""
                                if mode == "auto":
                                    do_rewrite, reason = _needs_rewrite(parsed_try.get("analysis_text"), parsed_try.get("items"))
                                if do_rewrite:
                                    try:
                                        new_txt = await _compose_analysis_text(parsed_try.get("items"), parsed_try.get("appearance") or {}, parsed_try.get("confidence"))
                                        if new_txt:
                                            parsed_try["analysis_text"] = new_txt
                                            foodai_analysis_text_rewrite.labels(reason=reason or "auto").inc()
                                        else:
                                            foodai_analysis_text_rewrite.labels(reason or "error").inc()
                                    except Exception:
                                        try:
                                            foodai_analysis_text_rewrite.labels("error").inc()
                                        except Exception:
                                            pass
                                try:
                                    if parsed_try.get("analysis_text"):
                                        txt = _sanitize_first_sentence(parsed_try.get("items"), str(parsed_try.get("analysis_text") or ""))
                                        if txt:
                                            if len(txt) > 420:
                                                txt = txt[:417].rstrip() + "…"
                                            parsed_try["analysis_text"] = txt
                                except Exception:
                                    pass
                                # chain stop metrics
                                try:
                                    total_ms = (time.perf_counter() - _chain_t0) * 1000.0
                                    foodai_escalation_total_dur_ms.observe(total_ms)
                                except Exception:
                                    pass
                                # Log final model used for this photo analysis (for debugging/UX visibility)
                                try:
                                    logger.info(
                                        "foodai.final | model={} | detail={} | steps={} | total_ms={} | conf={} | items={}",
                                        mid,
                                        _norm_detail(det),
                                        steps,
                                        int(total_ms),
                                        parsed_try.get("confidence"),
                                        len(list(parsed_try.get("items") or [])),
                                    )
                                except Exception:
                                    pass
                                # attach escalation meta for UX analytics
                                try:
                                    esc = {
                                        "enabled": True,
                                        "chain": model_chain,
                                        "detail_order": detail_seq_norm,
                                        "steps": steps,
                                        "final_model": mid,
                                        "stopped_reason": ("strong" if not _is_weak(parsed_try) else ("steps_exhausted" if (steps >= max_steps or last_step) else "weak")),
                                        "total_ms": int(total_ms),
                                    }
                                    m = parsed_try.get("meta") or {}
                                    m["escalation"] = esc
                                    parsed_try["meta"] = m
                                except Exception:
                                    pass
                                try:
                                    if not _is_weak(parsed_try):
                                        foodai_escalation_success.labels(
                                            from_model=(model_chain[midx - 1] if midx > 0 else "start"),
                                            to_model=mid,
                                            reason="strong",
                                        ).inc()
                                    else:
                                        reason_lbl = "steps_exhausted" if (steps >= max_steps or last_step) else "weak"
                                        foodai_escalation_failed.labels(
                                            from_model=(model_chain[midx - 1] if midx > 0 else "start"),
                                            to_model=mid,
                                            reason=reason_lbl,
                                        ).inc()
                                except Exception:
                                    pass
                                return parsed_try
                        if steps >= max_steps:
                            break
                    if steps >= max_steps:
                        break
                if best:
                    # exhausted chain, returning best available (weak)
                    try:
                        total_ms = (time.perf_counter() - _chain_t0) * 1000.0
                        foodai_escalation_total_dur_ms.observe(total_ms)
                    except Exception:
                        pass
                    try:
                        foodai_escalation_failed.labels(
                            from_model=(model_chain[-2] if len(model_chain) >= 2 else "start"),
                            to_model=(model_chain[-1] if len(model_chain) >= 1 else "none"),
                            reason="exhausted",
                        ).inc()
                    except Exception:
                        pass
                    # attach escalation meta
                    try:
                        esc = {
                            "enabled": True,
                            "chain": model_chain,
                            "detail_order": detail_seq_norm,
                            "steps": steps,
                            "final_model": model_chain[-1],
                            "stopped_reason": "exhausted",
                            "total_ms": int(total_ms),
                        }
                        m = best.get("meta") or {}
                        m["escalation"] = esc
                        best["meta"] = m
                    except Exception:
                        pass
                    return best

            initial_detail = _norm_detail(getattr(settings, "FOODAI_IMAGE_DETAIL", "low"))
            vision_model = getattr(settings, "FOODAI_VISION_MODEL", None) or settings.FOODAI_DEFAULT_MODEL
            parsed = await _build_and_call(initial_detail, vision_model)
            if parsed:
                conf = float(parsed.get("confidence") or 0)
                need_retry = (
                    bool(getattr(settings, "FOODAI_IMAGE_DETAIL_HIGH_RETRY", True))
                    and initial_detail != "high"
                    and conf < float(getattr(settings, "FOODAI_CONFIDENCE_ESCALATE", 0.7))
                )
                if need_retry:
                    parsed_hi = await _build_and_call("high", vision_model)
                    if parsed_hi:
                        try:
                            m = parsed_hi.get("meta") or {}
                            m["escalation"] = {"enabled": False}
                            parsed_hi["meta"] = m
                        except Exception:
                            pass
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
                try:
                    m = parsed.get("meta") or {}
                    m["escalation"] = {"enabled": False}
                    parsed["meta"] = m
                except Exception:
                    pass
                return parsed
            else:
                # parsing failed — try a single high-detail retry if enabled
                if bool(getattr(settings, "FOODAI_IMAGE_DETAIL_HIGH_RETRY", True)) and initial_detail != "high":
                    parsed_hi = await _build_and_call("high", vision_model)
                    if parsed_hi:
                        try:
                            m = parsed_hi.get("meta") or {}
                            m["escalation"] = {"enabled": False}
                            parsed_hi["meta"] = m
                        except Exception:
                            pass
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
        "instructions": system,
        "reasoning": {"effort": settings.FOODAI_REASONING_EFFORT},
        "text": {
            "verbosity": settings.FOODAI_TEXT_VERBOSITY,
            "format": {
                "type": "json_schema",
                "name": "foodness",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"is_food": {"type": "boolean"}},
                    "required": ["is_food"],
                    "additionalProperties": False
                }
            }
        },
        "max_output_tokens": 50,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Does this image contain food or drink?"},
                    {"type": "input_image", "image_url": file_url},
                ],
            }
        ],
    }
    try:
        # Local timeout for precheck to avoid waiting full FOODAI_TIMEOUT
        try:
            precheck_timeout = int(getattr(settings, "FOODAI_PRECHECK_TIMEOUT", 8) or 8)
        except Exception:
            precheck_timeout = 8
        content = await asyncio.wait_for(_openai_request("responses", payload), timeout=precheck_timeout)
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
            "carbs_g(float), weight_g(float), confidence(float 0..1), items(list of {name, calories, protein_g, fat_g, carbs_g, weight_g, is_liquid:boolean}), references({sources: [string]}), analysis_text(string), appearance({is_packaged: boolean, plate_visible: boolean, plate_diameter_cm: int|null}), not_food(boolean).\n"
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
                "max_output_tokens": 700,
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
            # Fallback to Chat API if Responses API failed
            if not content and bool(getattr(settings, "FOODAI_TEXT_FALLBACK_TO_CHAT", True)):
                payload_chat = {
                    "model": settings.FOODAI_DEFAULT_MODEL,
                    "temperature": 0,
                    "max_tokens": 600,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"{text}\n\nReturn JSON only."},
                    ],
                }
                content = await _openai_request("chat", payload_chat)
        else:
            payload = {
                "model": settings.FOODAI_DEFAULT_MODEL,
                "temperature": 0,
                "max_tokens": 600,
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
                                # re-cap length after sanitation
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

    instr_raw = (instruction or "").strip()
    instr = instr_raw.lower()
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
        # additions for better spoon/cup conversions
        "мёд": 1.42,
        "сироп": 1.30,
        "соевый соус": 1.20,
        "томатная паста": 1.08,
        "майонез": 0.90,
        "сметана": 0.99,
        "подсолнечное масло": 0.92,
        "масло подсолнечное": 0.92,
        "оливковое масло": 0.91,
    }
    pcs = {  # rough grams per piece
        "яйцо": 50,
        "яйц": 50,  # match 'яйца', 'яйца вареные' etc.
        "ломтик хлеба": 30,
        "сырок": 40,
        "яблоко": 180,
        "банан": 120,
        "помидор черри": 15,
        "томат черри": 15,
        "черри": 15,
        "помидор": 120,
        "томат": 120,
        "огурец": 120,
        "лук": 100,
        "зубчик чеснока": 5,
        "булочка": 50,
        "батон": 30,
        "лимон": 120,
        "апельсин": 180,
        "мандарин": 90,
        "груша": 170,
        "перец": 120,
        "морковь": 70,
        "луковица": 100,
        "кусок сыра": 20,
        # additions
        "шампиньон": 15,
        "оливка": 5,
        "маслина": 4,
        "долька лимона": 12,
        "долька лайма": 10,
        # extras
        "перепелиное яйцо": 10,
        "яйцо перепелиное": 10,
        "лайм": 70,
        "томат сливка": 80,
        "помидор сливка": 80,
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

    def _find_indices(items: list[dict[str, Any]], needle: str) -> list[int]:
        res: list[int] = []
        n_raw = (needle or "").strip()
        def _norm(s: str) -> str:
            s = (s or "").lower()
            try:
                s = s.replace("ё", "е")
            except Exception:
                pass
            return s
        def _tokens(s: str) -> list[str]:
            try:
                return re.findall(r"[a-zа-я]+", _norm(s))
            except Exception:
                return []
        def _stem(w: str) -> str:
            w = _norm(w)
            while len(w) > 3 and (w[-1] in "аеёиоуыэюяьй"):
                w = w[:-1]
            return w
        # minimal stop-words and descriptors to ignore in matching
        stop = {
            "жареный","жареная","жареное","жаренный","жаренные",
            "вареный","варёный","вареная","варёная","вареное","варёное",
            "пшеничный","овощной","говяжий","куриный","свиной","индюшиный",
            "копченый","копчёный","гриль","домашний","магазинный",
            "на","без","и","с","в","из","по","для","или","при",
            "бульоне","соусе","масле","воде",
        }
        # lightweight token synonyms (very small set for quick-fix)
        syn = {
            "картошка": "картофель",
            "картофел": "картофель",
            "томаты": "помидор",
            "томат": "помидор",
            "яйца": "яйцо",
            "яиц": "яйцо",
            "паста": "макароны",
            "лапша": "макароны",
        }
        def _map_token(t: str) -> str:
            t0 = _norm(t)
            return syn.get(t0, t0)
        n = _norm(n_raw)
        # direct substring path kept first
        for i, it in enumerate(items):
            try:
                name = _norm(str(it.get("name") or ""))
                if n and n in name:
                    res.append(i)
            except Exception:
                continue
        if res:
            return res
        # token-based fallback matching
        n_toks = [_map_token(t) for t in _tokens(n_raw) if _map_token(t) not in stop]
        n_stems = [_stem(t) for t in n_toks if t]
        for i, it in enumerate(items):
            try:
                name = _norm(str(it.get("name") or ""))
                if not name:
                    continue
                cand_toks = [_map_token(t) for t in _tokens(name) if _map_token(t) not in stop]
                cand_stems = [_stem(t) for t in cand_toks if t]
                if not n_stems:
                    continue
                # rule 1: every needle stem matches some candidate stem by inclusion
                all_hit = True
                for a in n_stems:
                    hit = False
                    for b in cand_stems:
                        if not a or not b:
                            continue
                        if (a == b) or (len(a) >= 3 and (a in b or b in a)):
                            hit = True
                            break
                    if not hit:
                        all_hit = False
                        break
                if all_hit:
                    res.append(i)
                    continue
                # rule 2: Jaccard-like overlap on stems
                if cand_stems:
                    sa = set(n_stems)
                    sb = set(cand_stems)
                    inter = 0
                    for a in sa:
                        for b in sb:
                            if (a == b) or (len(a) >= 3 and (a in b or b in a)):
                                inter += 1
                                break
                    denom = max(1, len(sa | sb))
                    if (inter / denom) >= 0.7:
                        res.append(i)
            except Exception:
                continue
        return res

    # --- RU morphology lite helpers (heuristics, no heavy deps) ---
    def _normalize_name_ru(name: str) -> str:
        s = (name or "").strip()
        if not s:
            return s
        s_l = s.lower()
        # quick known accusative -> nominative fixes
        mapping = {
            "рыбу": "рыба",
            "курицу": "курица",
            "говядину": "говядина",
            "свинину": "свинина",
            "индейку": "индейка",
            "утку": "утка",
            "пиццу": "пицца",
            "морковку": "морковь",
            "капусту": "капуста",
            "картошку": "картофель",
            "картошечку": "картофель",
            "колбасу": "колбаса",
            "гречку": "гречка",
            "кашу": "каша",
            "манку": "манка",
            "рыбку": "рыба",
            "соли": "соль",
            "солью": "соль",
        }
        # apply token-level mapping first
        try:
            parts = s_l.split()
            parts = [mapping.get(p, p) for p in parts]
            s_l = " ".join(parts)
        except Exception:
            pass
        # generic -у/-ю -> -а/-я
        if s_l.endswith("у") and len(s_l) >= 3:
            s_l = s_l[:-1] + "а"
        elif s_l.endswith("ю") and len(s_l) >= 3:
            s_l = s_l[:-1] + "я"
        # normalize spaces
        s_l = re.sub(r"\s+", " ", s_l)
        # capitalise product names minimally (first letter only)
        try:
            return s_l[0].upper() + s_l[1:]
        except Exception:
            return s_l

    # [removed] Title appending logic is removed per product decision.

    # --- Pre-parse: deterministic scale (times/percent) ---
    try:
        text_l = (instr_raw or "").lower()
        factor_pre: float | None = None
        dec = re.search(r"\b(уменьш|сократ|меньш|сниз|пониз|убав)\w*", text_l) is not None
        inc = re.search(r"\bувелич\w*", text_l) is not None

        # 1) 'в K раз(а)'
        mt = re.search(r"\bв\s*(\d+(?:[\.,]\d+)?)\s*раз[а]?\b", text_l)
        if mt and factor_pre is None:
            k = float(mt.group(1).replace(",", "."))
            factor_pre = (1.0 / k) if dec else (k if inc or not dec else None)

        # 1.1) 'в полтора раза'
        if factor_pre is None:
            if re.search(r"\bв\s+полтора\s+раз[а]?\b", text_l):
                k = 1.5
                factor_pre = (1.0 / k) if dec else k

        # 1.2) 'x2' / 'х2' без 'раза'
        if factor_pre is None:
            mx = re.search(r"(?:\bx|\bх)\s*(\d+(?:[\.,]\d+)?)\b", text_l)
            if mx:
                k = float(mx.group(1).replace(",", "."))
                factor_pre = (1.0 / k) if dec else (k if inc or not dec else None)

        # 1.3) слова: 'вдвое/втрое/вчетверо/впятеро/вшестеро/вдесятеро'
        if factor_pre is None:
            words_map = {
                "вдвое": 2.0,
                "втрое": 3.0,
                "вчетверо": 4.0,
                "впятеро": 5.0,
                "вшестеро": 6.0,
                "вдесятеро": 10.0,
            }
            for w, k in words_map.items():
                if re.search(rf"\b{w}\b", text_l):
                    factor_pre = (1.0 / k) if dec else (k if inc or not dec else None)
                    break

        # 1.4) 'наполовину' и 'на <долю>'
        if factor_pre is None:
            if re.search(r"\bнаполовину\b", text_l):
                # по умолчанию трактуем как 0.5; при явном увеличении — 1.5
                factor_pre = 0.5 if (dec or not inc) else 1.5
        if factor_pre is None:
            mf = re.search(r"\bна\s+(половину|треть|четверть|пятую|десятую)\b", text_l)
            if mf:
                frac_map = {
                    "половину": 1/2,
                    "треть": 1/3,
                    "четверть": 1/4,
                    "пятую": 1/5,
                    "десятую": 1/10,
                }
                f = float(frac_map.get(mf.group(1), 0.0))
                if f > 0:
                    if dec:
                        factor_pre = 1.0 - f
                    elif inc:
                        factor_pre = 1.0 + f
                    else:
                        factor_pre = None  # без явного направления не гадаем

        # 2) Проценты: '+30%', '-30%', юникод '−'/'–', и 'на 30%'
        if factor_pre is None:
            mp = re.search(r"([+\-−–]?\d{1,3})\s*%", text_l)
            if mp:
                p = float((mp.group(1) or "0").replace("−", "-").replace("–", "-"))
                if p < 0:
                    factor_pre = 1.0 + (p / 100.0)
                else:
                    is_dec = re.search(r"\b(уменьш|сократ|меньш)\w*", text_l) is not None
                    factor_pre = 1.0 - (p / 100.0) if is_dec else 1.0 + (p / 100.0)
        if factor_pre is not None:
            try:
                factor_pre = max(0.25, min(3.0, float(factor_pre)))
            except Exception:
                factor_pre = 1.0
            items = _clone_items(items_in)
            for it in items:
                try:
                    old_w = float(it.get("weight_g") or 0)
                    if it.get("weight_g") is not None:
                        it["weight_g"] = round(old_w * factor_pre, 1)
                    # scale macros if present; otherwise estimate anew
                    has_macros = (
                        it.get("calories") is not None and it.get("protein_g") is not None
                        and it.get("fat_g") is not None and it.get("carbs_g") is not None
                    )
                    if has_macros:
                        it["calories"] = int(round(float(it.get("calories") or 0) * factor_pre))
                        it["protein_g"] = round(float(it.get("protein_g") or 0) * factor_pre, 1)
                        it["fat_g"] = round(float(it.get("fat_g") or 0) * factor_pre, 1)
                        it["carbs_g"] = round(float(it.get("carbs_g") or 0) * factor_pre, 1)
                    else:
                        n = str(it.get("name") or "")
                        w = float(it.get("weight_g") or 0)
                        cal, p, f, c = _estimate_from_name(n, w)
                        it.update({"calories": cal, "protein_g": p, "fat_g": f, "carbs_g": c})
                except Exception:
                    continue
            out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
            # Scale total meal weight from base weight as ground truth; ensure it is not
            # lower than the sum of scaled item weights. This keeps UX consistent when
            # base weight != sum(items.weight_g).
            try:
                base_w = float(weight_g or 0.0)
            except Exception:
                base_w = 0.0
            try:
                base_scaled_w = round(base_w * float(factor_pre), 1)
            except Exception:
                base_scaled_w = 0.0
            wg_items_scaled = round(out_w, 1)
            wg_out = base_scaled_w if base_scaled_w > 0 else wg_items_scaled
            if wg_items_scaled > wg_out:
                wg_out = wg_items_scaled
            try:
                logger.info("FoodAI:scale | path=pre | instr='{}' | factor_pre={}", instr_raw, factor_pre)
            except Exception:
                pass
            return {
                "title": title or "Блюдо",
                "calories": int(out_cal if out_cal > 0 else int(round(base_cal * factor_pre))),
                "protein_g": float(out_p if out_p > 0 else round(base_p * factor_pre, 1)),
                "fat_g": float(out_f if out_f > 0 else round(base_f * factor_pre, 1)),
                "carbs_g": float(out_c if out_c > 0 else round(base_c * factor_pre, 1)),
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
                "meta": {"action": "scale", "delta_cal": int((out_cal if out_cal > 0 else int(round(base_cal * factor_pre))) - base_cal)},
            }
    except Exception:
        pass

    # --- unit conversion helper (must be defined before NLU block) ---
    def _qty_to_grams(name: str, qty: float, unit: str | None) -> tuple[float, bool]:
        unit_l = (unit or "г").lower()
        if unit_l in {"г", "гр", "грамм", "gram", "g"}:
            return float(qty), False
        if unit_l in {"кг", "kg"}:
            return float(qty) * 1000.0, False
        # spoons/cups -> ml
        if unit_l in {"ч.л", "ч.л.", "ч л", "чайная ложка", "чайная", "чайные ложки", "teaspoon"}:
            ml = float(qty) * 5.0
            return _qty_to_grams(name, ml, "мл")
        if unit_l in {"ст.л", "ст.л.", "ст л", "столовая ложка", "столовая", "столовые ложки", "tablespoon"}:
            ml = float(qty) * 15.0
            return _qty_to_grams(name, ml, "мл")
        if unit_l in {"стакан", "кружка", "чашка", "cup"}:
            ml = float(qty) * 250.0
            return _qty_to_grams(name, ml, "мл")
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
        # conversational units
        if unit_l in {"щепотка", "щепотку", "щеп", "pinch"}:
            n = (name or "").lower()
            # spices/salt default ~0.4 g
            w = 0.4
            return w * float(max(qty, 1.0)), False
        if unit_l in {"горсть", "горсти", "handful"}:
            n = (name or "").lower()
            if any(k in n for k in ["орех", "миндал", "грецк", "фундук", "кешью", "арахис", "фисташ", "семеч", "тыквен", "кунжут"]):
                return 30.0 * float(max(qty, 1.0)), False
            if any(k in n for k in ["петруш", "укроп", "базилик", "кинза", "руккол", "шпинат", "зелень"]):
                return 12.0 * float(max(qty, 1.0)), False
            return 30.0 * float(max(qty, 1.0)), False
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
            # eggs (boiled, average): ~155 kcal, 13P/11F/1.1C per 100 g
            "яйц": {"cal": 155, "p": 13.0, "f": 11.0, "c": 1.1},
            # common foods (approximate, per 100 g)
            "банан": {"cal": 89, "p": 1.1, "f": 0.3, "c": 22.8},
            "помидор": {"cal": 18, "p": 0.9, "f": 0.2, "c": 3.9},
            "томат": {"cal": 18, "p": 0.9, "f": 0.2, "c": 3.9},
            "огур": {"cal": 16, "p": 0.8, "f": 0.1, "c": 3.6},
            "лук": {"cal": 40, "p": 1.1, "f": 0.1, "c": 9.3},
            "чеснок": {"cal": 149, "p": 6.4, "f": 0.5, "c": 33.0},
            "картоф": {"cal": 87, "p": 1.9, "f": 0.1, "c": 20.1},
            "морков": {"cal": 41, "p": 0.9, "f": 0.2, "c": 10.0},
            "яблок": {"cal": 52, "p": 0.3, "f": 0.2, "c": 14.0},
            "хлеб": {"cal": 265, "p": 9.0, "f": 3.2, "c": 49.0},
            # cooked cereals/pasta typical values
            "рис": {"cal": 130, "p": 2.7, "f": 0.3, "c": 28.0},
            "греч": {"cal": 110, "p": 3.6, "f": 1.7, "c": 20.0},
            "макарон": {"cal": 158, "p": 5.8, "f": 0.9, "c": 30.0},
            "паста": {"cal": 158, "p": 5.8, "f": 0.9, "c": 30.0},
            # beverages/dairy (per 100 g)
            "кефир": {"cal": 50, "p": 3.0, "f": 2.5, "c": 4.0},    # 2.5–3.2% fat
            "йогурт": {"cal": 60, "p": 3.5, "f": 3.0, "c": 4.7},  # plain, unsweetened
            "молок": {"cal": 60, "p": 3.2, "f": 3.2, "c": 4.8},   # stem to match "молоко"
            "ряженк": {"cal": 54, "p": 2.9, "f": 2.5, "c": 4.1},
            "простокваш": {"cal": 56, "p": 3.0, "f": 2.5, "c": 4.3},
            "квас": {"cal": 27, "p": 0.2, "f": 0.0, "c": 6.0},
            "вино": {"cal": 85, "p": 0.1, "f": 0.0, "c": 2.6},
            "пиво": {"cal": 43, "p": 0.4, "f": 0.0, "c": 3.6},
            "сироп": {"cal": 260, "p": 0.0, "f": 0.0, "c": 65.0},
            "мёд": {"cal": 304, "p": 0.3, "f": 0.0, "c": 82.0},
            "сок": {"cal": 45, "p": 0.5, "f": 0.1, "c": 10.0},    # generic juice avg
            "лимонад": {"cal": 40, "p": 0.0, "f": 0.0, "c": 10.0},
            # bread/bakery/sweets
            "ржан": {"cal": 210, "p": 5.0, "f": 1.3, "c": 44.0},  # хлеб ржаной
            "лаваш": {"cal": 260, "p": 8.0, "f": 1.2, "c": 52.0},
            "сахар": {"cal": 387, "p": 0.0, "f": 0.0, "c": 100.0},
            "арахис": {"cal": 588, "p": 21.0, "f": 50.0, "c": 20.0},  # паста
            "шоколад": {"cal": 546, "p": 4.9, "f": 31.0, "c": 61.0},
            # meats/fish (cooked/lean generalizations)
            "курин груд": {"cal": 165, "p": 31.0, "f": 3.6, "c": 0.0},
            "курин бедр": {"cal": 209, "p": 26.0, "f": 10.9, "c": 0.0},
            "индейк фил": {"cal": 135, "p": 29.0, "f": 1.0, "c": 0.0},
            "говядин": {"cal": 187, "p": 26.0, "f": 8.0, "c": 0.0},
            "свинин": {"cal": 242, "p": 27.0, "f": 14.0, "c": 0.0},
            "лосос": {"cal": 208, "p": 20.0, "f": 13.0, "c": 0.0},
            "тунец": {"cal": 132, "p": 29.0, "f": 1.0, "c": 0.0},
            "сельд": {"cal": 248, "p": 25.0, "f": 15.0, "c": 0.0},
            "ветчин": {"cal": 145, "p": 20.0, "f": 6.0, "c": 1.5},
            "сосиск": {"cal": 270, "p": 12.0, "f": 24.0, "c": 2.0},
            # grains cooked
            "овсян": {"cal": 68, "p": 2.4, "f": 1.4, "c": 12.0},
            "пшён": {"cal": 109, "p": 3.2, "f": 1.0, "c": 23.0},
            "перлов": {"cal": 110, "p": 3.5, "f": 0.4, "c": 23.0},
            "булгур": {"cal": 110, "p": 3.1, "f": 0.3, "c": 22.9},
            "кускус": {"cal": 112, "p": 3.8, "f": 0.2, "c": 23.2},
            "пельмен": {"cal": 245, "p": 10.0, "f": 12.0, "c": 24.0},
            # nuts/seeds/fruits
            "миндал": {"cal": 579, "p": 21.0, "f": 50.0, "c": 22.0},
            "грецк": {"cal": 654, "p": 15.0, "f": 65.0, "c": 14.0},
            "фундук": {"cal": 628, "p": 15.0, "f": 61.0, "c": 17.0},
            "кешью": {"cal": 553, "p": 18.0, "f": 44.0, "c": 30.0},
            "семечки": {"cal": 584, "p": 21.0, "f": 51.0, "c": 20.0},
            "чиа": {"cal": 486, "p": 17.0, "f": 31.0, "c": 44.0},
            "авокадо": {"cal": 160, "p": 2.0, "f": 15.0, "c": 9.0},
            # herbs/greens (per 100 g, generic values)
            "петруш": {"cal": 36, "p": 3.0, "f": 0.8, "c": 6.3},   # петрушка
            "укроп": {"cal": 43, "p": 3.5, "f": 1.1, "c": 7.0},     # укроп
            "базилик": {"cal": 23, "p": 3.1, "f": 0.6, "c": 2.7},  # базилик
            "кинза": {"cal": 23, "p": 2.1, "f": 0.5, "c": 3.7},    # кинза/кориандр зелень
            "руккол": {"cal": 25, "p": 2.6, "f": 0.7, "c": 3.7},   # руккола
            "шпинат": {"cal": 23, "p": 2.9, "f": 0.4, "c": 3.6},   # шпинат
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

    # --- LLM NLU first (optional) ---
    if getattr(settings, "FOODAI_EDIT_NLU", False) and settings.OPENAI_API_KEY and interpret_edit is not None:
        try:
            # Provide compact base for NLU
            base_for_nlu = {
                "title": title,
                "weight_g": weight_g,
                "items": [{"name": it.get("name"), "weight_g": it.get("weight_g")} for it in items_in][:20],
            }
            nlu = await interpret_edit(base_for_nlu, instr_raw)
        except Exception:
            nlu = {"error": "nlu_error"}
        if isinstance(nlu, dict) and not nlu.get("error"):
            action = str(nlu.get("action") or "unknown")
            # Map to deterministic apply
            if action == "add":
                add_name = _normalize_name_ru(str(nlu.get("target") or "").strip())
                qty = float(nlu.get("qty_g") or 0)
                unit = str(nlu.get("unit") or "g")
                items = _clone_items(items_in)
                qty_g, _ = _qty_to_grams(add_name, qty, unit)
                qty_g = max(1.0, min(1000.0, qty_g))
                cal, p, f, c = _estimate_from_name(add_name, qty_g)
                items.append({"name": add_name, "weight_g": qty_g, "calories": cal, "protein_g": p, "fat_g": f, "carbs_g": c})
                out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
                new_cal = int(max(out_cal, base_cal + cal))
                return {
                    "title": (title or "Блюдо"),
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
            if action == "remove":
                name = _normalize_name_ru(str(nlu.get("target") or "").strip())
                items = _clone_items(items_in)
                idxs = _find_indices(items, name)
                if len(idxs) != 1:
                    reason = "not_found" if len(idxs) == 0 else "ambiguous"
                    return {"error": reason, "meta": {"action": action, "reason": reason}}
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
            if action == "replace":
                old_name = _normalize_name_ru(str(nlu.get("target") or "").strip())
                new_name = _normalize_name_ru(str(nlu.get("replacement") or "").strip())
                qty = nlu.get("qty_g")
                unit = nlu.get("unit")
                items = _clone_items(items_in)
                idxs = _find_indices(items, old_name)
                if len(idxs) != 1:
                    reason = "not_found" if len(idxs) == 0 else "ambiguous"
                    return {"error": reason, "meta": {"action": action, "reason": reason}}
                idx = idxs[0]
                try:
                    old_item = items.pop(idx)
                except Exception:
                    old_item = None
                if qty is not None:
                    new_qty_g, _ = _qty_to_grams(new_name, float(qty), unit)
                else:
                    try:
                        new_qty_g = float((old_item or {}).get("weight_g") or 100.0)
                    except Exception:
                        new_qty_g = 100.0
                # caps
                new_qty_g = max(1.0, min(1000.0, new_qty_g))
                cal, p, f, c = _estimate_from_name(new_name, new_qty_g)
                _item_out = {
                    "name": new_name,
                    "weight_g": new_qty_g,
                    "calories": cal,
                    "protein_g": p,
                    "fat_g": f,
                    "carbs_g": c,
                }
                # Preserve original unit for UI if user specified qty+unit
                try:
                    if qty is not None and unit is not None:
                        u = (str(unit) or "").lower()
                        app = None
                        if u in {"мл", "ml"}:
                            d = 1.0
                            key = (new_name or "").lower()
                            for k, val in densities.items():
                                if k in key:
                                    d = val
                                    break
                            app = {"unit": "ml", "qty": float(qty), "density": float(d), "approx_g": float(new_qty_g)}
                            _item_out["is_liquid"] = True
                        elif u in {"л", "l"}:
                            app = {"unit": "l", "qty": float(qty), "density": 1.0, "approx_g": float(new_qty_g)}
                            _item_out["is_liquid"] = True
                        elif u in {"шт", "pc", "pcs"}:
                            app = {"unit": "шт", "qty": float(qty), "approx_g": float(new_qty_g)}
                        if app:
                            _item_out["appearance"] = app
                except Exception:
                    pass
                try:
                    # If converter told us it's liquid — mark it
                    if qty is not None and '_is_liq' in locals() and bool(_is_liq):
                        _item_out["is_liquid"] = True
                except Exception:
                    pass
                items.append(_item_out)
                out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
                try:
                    logger.info("FoodAI:edit | replace | ok | old='{}' new='{}' | new_qty_g={} | delta_cal={}", old_name, new_name, new_qty_g, int(out_cal - base_cal))
                except Exception:
                    pass
                return {
                    "title": (title or "Блюдо"),
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
            if action == "change_qty":
                name = _normalize_name_ru(str(nlu.get("target") or "").strip())
                qty = nlu.get("qty_g")
                unit = nlu.get("unit")
                items = _clone_items(items_in)
                idxs = _find_indices(items, name)
                if len(idxs) != 1:
                    reason = "not_found" if len(idxs) == 0 else "ambiguous"
                    return {"error": reason, "meta": {"action": action, "reason": reason}}
                idx = idxs[0]
                new_qty_g, is_liq = _qty_to_grams(name, float(qty), unit)
                new_qty_g = max(1.0, min(1000.0, new_qty_g))
                cal, p, f, c = _estimate_from_name(name, new_qty_g)
                _upd = {
                    "weight_g": new_qty_g,
                    "calories": cal,
                    "protein_g": p,
                    "fat_g": f,
                    "carbs_g": c,
                }
                try:
                    if bool(is_liq):
                        _upd["is_liquid"] = True
                    if qty is not None and unit is not None:
                        u = (str(unit) or "").lower()
                        app = None
                        if u in {"мл", "ml"}:
                            d = 1.0
                            key = (name or "").lower()
                            for k, val in densities.items():
                                if k in key:
                                    d = val
                                    break
                            app = {"unit": "ml", "qty": float(qty), "density": float(d), "approx_g": float(new_qty_g)}
                            _upd["is_liquid"] = True
                        elif u in {"л", "l"}:
                            app = {"unit": "l", "qty": float(qty), "density": 1.0, "approx_g": float(new_qty_g)}
                            _upd["is_liquid"] = True
                        elif u in {"шт", "pc", "pcs"}:
                            app = {"unit": "шт", "qty": float(qty), "approx_g": float(new_qty_g)}
                        if app:
                            _upd["appearance"] = app
                except Exception:
                    pass
                items[idx].update(_upd)
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
            if action == "scale":
                factor = float(nlu.get("factor") or 1.0)
                # Try to override factor based on explicit phrasing
                try:
                    text_l = (instr_raw or "").lower()
                    # 1) 'в K раз(а)' pattern
                    mt = re.search(r"(?:\bв|\bx|\bх)\s*(\d+(?:[\.,]\d+)?)\s*раз[а]?\b", text_l)
                    if mt:
                        k = float(mt.group(1).replace(",", "."))
                        if re.search(r"\b(уменьш|сократ|меньш)\w*", text_l):
                            factor = (1.0 / k) if k > 0 else 1.0
                        elif re.search(r"\bувелич\w*", text_l):
                            factor = k
                        # else keep LLM factor
                        try:
                            logger.info("FoodAI:scale | path=nlu | override=times | k={} | factor_final={}", k, factor)
                        except Exception:
                            pass
                    else:
                        # 2) percent pattern like +30%, -30%, 'на 30%'
                        mp = re.search(r"([+\-−–]?\d{1,3})\s*%", text_l)
                        if mp:
                            p = float((mp.group(1) or "0").replace("−", "-").replace("–", "-"))
                            if p < 0:
                                factor = 1.0 + (p / 100.0)
                            else:
                                is_dec = re.search(r"\b(уменьш|сократ|меньш)\w*", text_l) is not None
                                factor = 1.0 - (p / 100.0) if is_dec else 1.0 + (p / 100.0)
                            try:
                                logger.info("FoodAI:scale | path=nlu | override=percent | p={} | factor_final={}", p, factor)
                            except Exception:
                                pass
                    # 3) If user explicitly asked to decrease and factor>1 -> interpret as divide (safety)
                    if re.search(r"\b(уменьш|сократ|меньш)\w*", text_l) and factor > 1.0:
                        factor = 1.0 / factor
                except Exception:
                    pass
                # Clamp to reasonable bounds
                try:
                    factor = max(0.25, min(3.0, float(factor)))
                except Exception:
                    factor = 1.0
                # Debug log
                try:
                    logger.info("FoodAI:scale | path=nlu | instr='{}' | factor_final={}", instr_raw, factor)
                except Exception:
                    pass
                items = _clone_items(items_in)
                for it in items:
                    try:
                        old_w = float(it.get("weight_g") or 0)
                        if it.get("weight_g") is not None:
                            it["weight_g"] = round(old_w * factor, 1)
                        # If item already has macros — scale them directly; otherwise estimate
                        has_macros = (
                            it.get("calories") is not None and it.get("protein_g") is not None
                            and it.get("fat_g") is not None and it.get("carbs_g") is not None
                        )
                        if has_macros:
                            it["calories"] = int(round(float(it.get("calories") or 0) * factor))
                            it["protein_g"] = round(float(it.get("protein_g") or 0) * factor, 1)
                            it["fat_g"] = round(float(it.get("fat_g") or 0) * factor, 1)
                            it["carbs_g"] = round(float(it.get("carbs_g") or 0) * factor, 1)
                        else:
                            n = str(it.get("name") or "")
                            w = float(it.get("weight_g") or 0)
                            cal, p, f, c = _estimate_from_name(n, w)
                            it.update({"calories": cal, "protein_g": p, "fat_g": f, "carbs_g": c})
                    except Exception:
                        continue
                out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
                wg_out = round(out_w, 1)
                try:
                    logger.info("FoodAI:scale | path=nlu | instr='{}' | factor={} | out_w={}", instr_raw, factor, wg_out)
                except Exception:
                    pass
                return {
                    "title": title or "Блюдо",
                    "calories": int(out_cal if out_cal > 0 else int(round(base_cal * factor))),
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
                    "meta": {"action": action, "delta_cal": int((out_cal if out_cal > 0 else int(round(base_cal * factor))) - base_cal)},
                }

    def _qty_to_grams(name: str, qty: float, unit: str | None) -> tuple[float, bool]:
        unit_l = (unit or "г").lower()
        if unit_l in {"г", "гр", "грамм", "gram", "g"}:
            return float(qty), False
        if unit_l in {"кг", "kg"}:
            return float(qty) * 1000.0, False
        # spoons/cups -> ml
        if unit_l in {"ч.л", "ч.л.", "ч л", "чайная ложка", "чайная", "чайные ложки", "teaspoon"}:
            ml = float(qty) * 5.0
            return _qty_to_grams(name, ml, "мл")
        if unit_l in {"ст.л", "ст.л.", "ст л", "столовая ложка", "столовая", "столовые ложки", "tablespoon"}:
            ml = float(qty) * 15.0
            return _qty_to_grams(name, ml, "мл")
        if unit_l in {"стакан", "кружка", "чашка", "cup"}:
            ml = float(qty) * 250.0
            return _qty_to_grams(name, ml, "мл")
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
        # conversational units
        if unit_l in {"щепотка", "щепотку", "щеп", "pinch"}:
            # spices/salt default ~0.4 g
            w = 0.4
            return w * float(max(qty, 1.0)), False
        if unit_l in {"горсть", "горсти", "handful"}:
            n = (name or "").lower()
            if any(k in n for k in ["орех", "миндал", "грецк", "фундук", "кешью", "арахис", "фисташ", "семеч", "тыквен", "кунжут"]):
                return 30.0 * float(max(qty, 1.0)), False
            if any(k in n for k in ["петруш", "укроп", "базилик", "кинза", "руккол", "шпинат", "зелень"]):
                return 12.0 * float(max(qty, 1.0)), False
            return 30.0 * float(max(qty, 1.0)), False
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
            # eggs (boiled, average): ~155 kcal, 13P/11F/1.1C per 100 g
            "яйц": {"cal": 155, "p": 13.0, "f": 11.0, "c": 1.1},
            # common foods (approximate, per 100 g)
            "банан": {"cal": 89, "p": 1.1, "f": 0.3, "c": 22.8},
            "помидор": {"cal": 18, "p": 0.9, "f": 0.2, "c": 3.9},
            "томат": {"cal": 18, "p": 0.9, "f": 0.2, "c": 3.9},
            "огур": {"cal": 16, "p": 0.8, "f": 0.1, "c": 3.6},
            "лук": {"cal": 40, "p": 1.1, "f": 0.1, "c": 9.3},
            "чеснок": {"cal": 149, "p": 6.4, "f": 0.5, "c": 33.0},
            "картоф": {"cal": 87, "p": 1.9, "f": 0.1, "c": 20.1},
            "морков": {"cal": 41, "p": 0.9, "f": 0.2, "c": 10.0},
            "яблок": {"cal": 52, "p": 0.3, "f": 0.2, "c": 14.0},
            "хлеб": {"cal": 265, "p": 9.0, "f": 3.2, "c": 49.0},
            # cooked cereals/pasta typical values
            "рис": {"cal": 130, "p": 2.7, "f": 0.3, "c": 28.0},
            "греч": {"cal": 110, "p": 3.6, "f": 1.7, "c": 20.0},
            "макарон": {"cal": 158, "p": 5.8, "f": 0.9, "c": 30.0},
            "паста": {"cal": 158, "p": 5.8, "f": 0.9, "c": 30.0},
            # beverages/dairy (per 100 g)
            "кефир": {"cal": 50, "p": 3.0, "f": 2.5, "c": 4.0},    # 2.5–3.2% fat
            "йогурт": {"cal": 60, "p": 3.5, "f": 3.0, "c": 4.7},  # plain, unsweetened
            "молок": {"cal": 60, "p": 3.2, "f": 3.2, "c": 4.8},   # stem to match "молоко"
            "ряженк": {"cal": 54, "p": 2.9, "f": 2.5, "c": 4.1},
            "простокваш": {"cal": 56, "p": 3.0, "f": 2.5, "c": 4.3},
            "квас": {"cal": 27, "p": 0.2, "f": 0.0, "c": 6.0},
            "вино": {"cal": 85, "p": 0.1, "f": 0.0, "c": 2.6},
            "пиво": {"cal": 43, "p": 0.4, "f": 0.0, "c": 3.6},
            "сироп": {"cal": 260, "p": 0.0, "f": 0.0, "c": 65.0},
            "мёд": {"cal": 304, "p": 0.3, "f": 0.0, "c": 82.0},
            "сок": {"cal": 45, "p": 0.5, "f": 0.1, "c": 10.0},    # generic juice avg
            "лимонад": {"cal": 40, "p": 0.0, "f": 0.0, "c": 10.0},
            # bread/bakery/sweets
            "ржан": {"cal": 210, "p": 5.0, "f": 1.3, "c": 44.0},  # хлеб ржаной
            "лаваш": {"cal": 260, "p": 8.0, "f": 1.2, "c": 52.0},
            "сахар": {"cal": 387, "p": 0.0, "f": 0.0, "c": 100.0},
            "арахис": {"cal": 588, "p": 21.0, "f": 50.0, "c": 20.0},  # паста
            "шоколад": {"cal": 546, "p": 4.9, "f": 31.0, "c": 61.0},
            # meats/fish (cooked/lean generalizations)
            "курин груд": {"cal": 165, "p": 31.0, "f": 3.6, "c": 0.0},
            "курин бедр": {"cal": 209, "p": 26.0, "f": 10.9, "c": 0.0},
            "индейк фил": {"cal": 135, "p": 29.0, "f": 1.0, "c": 0.0},
            "говядин": {"cal": 187, "p": 26.0, "f": 8.0, "c": 0.0},
            "свинин": {"cal": 242, "p": 27.0, "f": 14.0, "c": 0.0},
            "лосос": {"cal": 208, "p": 20.0, "f": 13.0, "c": 0.0},
            "тунец": {"cal": 132, "p": 29.0, "f": 1.0, "c": 0.0},
            "сельд": {"cal": 248, "p": 25.0, "f": 15.0, "c": 0.0},
            "ветчин": {"cal": 145, "p": 20.0, "f": 6.0, "c": 1.5},
            "сосиск": {"cal": 270, "p": 12.0, "f": 24.0, "c": 2.0},
            "бекон": {"cal": 541, "p": 37.0, "f": 42.0, "c": 1.4},
            # grains cooked
            "овсян": {"cal": 68, "p": 2.4, "f": 1.4, "c": 12.0},
            "пшён": {"cal": 109, "p": 3.2, "f": 1.0, "c": 23.0},
            "перлов": {"cal": 110, "p": 3.5, "f": 0.4, "c": 23.0},
            "булгур": {"cal": 110, "p": 3.1, "f": 0.3, "c": 22.9},
            "кускус": {"cal": 112, "p": 3.8, "f": 0.2, "c": 23.2},
            "пельмен": {"cal": 245, "p": 10.0, "f": 12.0, "c": 24.0},
            # nuts/seeds/fruits
            "миндал": {"cal": 579, "p": 21.0, "f": 50.0, "c": 22.0},
            "грецк": {"cal": 654, "p": 15.0, "f": 65.0, "c": 14.0},
            "фундук": {"cal": 628, "p": 15.0, "f": 61.0, "c": 17.0},
            "кешью": {"cal": 553, "p": 18.0, "f": 44.0, "c": 30.0},
            "семечки": {"cal": 584, "p": 21.0, "f": 51.0, "c": 20.0},
            "чиа": {"cal": 486, "p": 17.0, "f": 31.0, "c": 44.0},
            "авокадо": {"cal": 160, "p": 2.0, "f": 15.0, "c": 9.0},
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
        """Find item indices by forgiving Russian matching.

        - Normalizes both the needle and item name via _normalize_name_ru.
        - Matches if either contains the other (substring, after normalization).
        - Falls back to: (a) root-token intersection (>=4 letters), (b) shared 4-letter token prefixes.
        """
        res: list[int] = []
        try:
            n_norm = (_normalize_name_ru(needle) or "").lower().strip()
        except Exception:
            n_norm = (needle or "").lower().strip()
        for i, it in enumerate(items):
            try:
                raw = str(it.get("name") or "")
                try:
                    nm = (_normalize_name_ru(raw) or "").lower()
                except Exception:
                    nm = raw.lower()
                cond = bool(n_norm) and (n_norm in nm or nm in n_norm)
                if not cond:
                    # token-root overlap (>=4 letters)
                    import re as _re
                    roots = set(_re.findall(r"[a-zа-яё]{4,}", n_norm))
                    if any(r in nm for r in roots):
                        cond = True
                if not cond:
                    # shared 4-letter token prefix (e.g., 'куриц' vs 'курин' -> 'кури')
                    import re as _re2
                    def _tok4(s: str) -> set[str]:
                        out = set()
                        for t in _re2.findall(r"[a-zа-яё]{4,}", s):
                            out.add(t[:4])
                        return out
                    pref_a = _tok4(n_norm)
                    pref_b = _tok4(nm)
                    if pref_a and pref_b and (pref_a & pref_b):
                        cond = True
                if cond:
                    res.append(i)
            except Exception:
                continue
        return res

    # --- parse order: replace > change_qty > add > remove > scale ---
    action = "unknown"
    reason = None

    # replace
    m = re.search(r"(?:заменить|замени|поменять)\s+([a-zа-яё\-\s]+?)\s+на\s+([a-zа-яё\-\s]+?)(?:\s+(\d{1,4})\s*(г|гр|грамм|мл|ml|л|l|шт|pc|pcs|ч\.л\.?|ст\.л\.?|стакан|кружка|чашка|щепотка|горсть))?\b", instr, flags=re.IGNORECASE)
    if m:
        action = "replace"
        old_name = _normalize_name_ru((m.group(1) or "").strip())
        new_name = _normalize_name_ru((m.group(2) or "").strip())
        qty = float(m.group(3)) if m.group(3) else None
        unit = (m.group(4) or None)
        items = _clone_items(items_in)
        idxs = _find_indices(items, old_name)
        if len(idxs) == 0:
            try:
                logger.info("FoodAI:edit | replace | match=0 | old='{}'", old_name)
            except Exception:
                pass
            return {"error": "not_found", "meta": {"action": action, "reason": "not_found"}}
        if len(idxs) > 1:
            try:
                logger.info("FoodAI:edit | replace | match>1 | idxs={} | old='{}'", idxs, old_name)
            except Exception:
                pass
            return {"error": "ambiguous", "meta": {"action": action, "reason": "ambiguous"}}
        idx = idxs[0]
        # Foodness gate for new item in replace
        try:
            is_food_flag: bool | None = None
            # quick lexical whitelist; if not decisive, ask LLM classifier
            lex_hit = _lexicon_is_food_text(new_name)
            if lex_hit is True:
                is_food_flag = True
            else:
                is_food_flag = await _foodness_text(new_name)
        except Exception:
            is_food_flag = None
        if is_food_flag is False:
            try:
                logger.info("FoodAI:edit | replace | rejected_not_food | new='{}'", new_name)
            except Exception:
                pass
            return {"error": "not_food", "meta": {"action": action, "reason": "not_food"}}
        # decide new qty
        new_qty_g: float
        if qty is not None:
            new_qty_g, _is_liq = _qty_to_grams(new_name, qty, unit)
        else:
            try:
                new_qty_g = float((items[idx] or {}).get("weight_g") or 100.0)
            except Exception:
                new_qty_g = 100.0
        # caps
        new_qty_g = max(1.0, min(1000.0, new_qty_g))
        cal, p, f, c = _estimate_from_name(new_name, new_qty_g)
        _item_out = {
            "name": new_name,
            "weight_g": new_qty_g,
            "calories": cal,
            "protein_g": p,
            "fat_g": f,
            "carbs_g": c,
        }
        # Preserve original unit for UI if user specified qty+unit
        try:
            if qty is not None and unit is not None:
                u = (str(unit) or "").lower()
                app = None
                if u in {"мл", "ml"}:
                    d = 1.0
                    key = (new_name or "").lower()
                    for k, val in densities.items():
                        if k in key:
                            d = val
                            break
                    app = {"unit": "ml", "qty": float(qty), "density": float(d), "approx_g": float(new_qty_g)}
                    _item_out["is_liquid"] = True
                elif u in {"л", "l"}:
                    app = {"unit": "l", "qty": float(qty), "density": 1.0, "approx_g": float(new_qty_g)}
                    _item_out["is_liquid"] = True
                elif u in {"шт", "pc", "pcs"}:
                    app = {"unit": "шт", "qty": float(qty), "approx_g": float(new_qty_g)}
                if app:
                    _item_out["appearance"] = app
        except Exception:
            pass
        try:
            # If converter told us it's liquid — mark it
            if qty is not None and '_is_liq' in locals() and bool(_is_liq):
                _item_out["is_liquid"] = True
        except Exception:
            pass
        items[idx] = _item_out
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        try:
            logger.info("FoodAI:edit | replace | ok | old='{}' new='{}' | new_qty_g={} | delta_cal={}", old_name, new_name, new_qty_g, int(out_cal - base_cal))
        except Exception:
            pass
        return {
            "title": (title or "Блюдо"),
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
    m = re.search(r"(?:увеличить|уменьшить|сделать|до)\s+([a-zа-яё\-\s]+?)\s*(?:до)?\s*(\+?\-?\d{1,4})\s*(г|гр|грамм|мл|ml|л|l|шт|pc|pcs|ч\.л\.?|ст\.л\.?|стакан|кружка|чашка|щепотка|горсть)\b", instr, flags=re.IGNORECASE)
    if not m:
        # pattern: '<name> +N г' but avoid leading action verbs (добавить/убрать/заменить)
        if re.match(r"\s*(?:добавить|добавь|положить|прибавить|убрать|убери|удалить|удали|без|минус|\-|заменить|замени|поменять)\b", instr, flags=re.IGNORECASE):
            m = None
        else:
            m = re.search(r"^([a-zа-яё\-\s]+?)\s*([\+\-]?\d{1,4})\s*(г|гр|грамм|мл|ml|л|l|шт|pc|pcs|ч\.л\.?|ст\.л\.?|стакан|кружка|чашка|щепотка|горсть)\b", instr, flags=re.IGNORECASE)
    if m:
        action = "change_qty"
        name = _normalize_name_ru((m.group(1) or "").strip())
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
        _item_out = {
            "name": name,
            "weight_g": new_qty_g,
            "calories": cal,
            "protein_g": p,
            "fat_g": f,
            "carbs_g": c,
        }
        # Preserve original unit for UI if user specified qty+unit
        try:
            if unit is not None:
                u = (str(unit) or "").lower()
                app = None
                if u in {"мл", "ml"}:
                    d = 1.0
                    key = (name or "").lower()
                    for k, val in densities.items():
                        if k in key:
                            d = val
                            break
                    app = {"unit": "ml", "qty": float(qty), "density": float(d), "approx_g": float(new_qty_g)}
                    _item_out["is_liquid"] = True
                elif u in {"л", "l"}:
                    app = {"unit": "l", "qty": float(qty), "density": 1.0, "approx_g": float(new_qty_g)}
                    _item_out["is_liquid"] = True
                elif u in {"шт", "pc", "pcs"}:
                    app = {"unit": "шт", "qty": float(qty), "approx_g": float(new_qty_g)}
                if app:
                    _item_out["appearance"] = app
        except Exception:
            pass
        try:
            # If converter told us it's liquid — mark it
            if unit is not None and '_is_liq' in locals() and bool(_is_liq):
                _item_out["is_liquid"] = True
        except Exception:
            pass
        items[idx] = _item_out
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

    # add (unit-first, e.g. 'добавь 1 ст.л. мёда' or 'добавь 1 стакан кефира')
    m = re.match(r"^\s*(?:добавить|добавь|положить|прибавить|\+)\s*(\d{1,4})\s*(ч\.л\.?|ст\.л\.?|стакан|кружка|чашка|мл|ml|л|l|шт|pc|pcs|щепотк(?:а|у|и)?|горст(?:ь|и)?)\s+([a-zа-яё\-\s]+?)\s*$", instr, flags=re.IGNORECASE)
    if m:
        action = "add"
        add_qty = float(m.group(1))
        unit = (m.group(2) or None)
        add_name = _normalize_name_ru((m.group(3) or "").strip())
        try:
            is_food_flag: bool | None = None
            lex_hit = _lexicon_is_food_text(add_name)
            if lex_hit is True:
                is_food_flag = True
            else:
                is_food_flag = await _foodness_text(add_name)
        except Exception:
            is_food_flag = None
        if is_food_flag is False:
            return {"error": "not_food", "meta": {"action": action, "reason": "not_food"}}
        qty_g, _is_liq = _qty_to_grams(add_name, add_qty, unit)
        qty_g = max(0.1, min(1000.0, qty_g))
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
        new_cal = int(max(out_cal, base_cal + add_cal))
        return {
            "title": (title or "Блюдо"),
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

    # add (pinch/handful without number e.g. 'добавь щепотку соли')
    m = re.match(r"^\s*(?:добавить|добавь|положить|прибавить|\+)\s*(щепотк(?:а|у|и)?|горст(?:ь|и)?)\s+([a-zа-яё\-\s]+?)\s*$", instr, flags=re.IGNORECASE)
    if m:
        action = "add"
        unit = (m.group(1) or None)
        add_name = _normalize_name_ru((m.group(2) or "").strip())
        try:
            is_food_flag = _lexicon_is_food_text(add_name)
            if is_food_flag is not True:
                is_food_flag = await _foodness_text(add_name)
        except Exception:
            is_food_flag = None
        if is_food_flag is False:
            return {"error": "not_food", "meta": {"action": action, "reason": "not_food"}}
        qty_g, _is_liq = _qty_to_grams(add_name, 1.0, unit)
        qty_g = max(0.1, min(1000.0, qty_g))
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
        new_cal = int(max(out_cal, base_cal + add_cal))
        return {
            "title": (title or "Блюдо"),
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

    # add
    # Anchor add at line start to avoid catching scale-like phrases; prefer explicit verbs or '+'
    m = re.match(r"^\s*(?:добавить|добавь|положить|прибавить|\+)\s*([a-zа-яё\-\s]+?)\s*(\d{1,4})\s*(г|гр|грамм|мл|ml|л|l|шт|pc|pcs|ч\.л\.?|ст\.л\.?|стакан|кружка|чашка|щепотка|горсть)?\b", instr, flags=re.IGNORECASE)
    if m:
        action = "add"
        add_name = _normalize_name_ru((m.group(1) or "").strip().strip('- '))
        add_name = re.sub(r"^(?:добавить|добавь|положить|прибавить)\s+", "", add_name, flags=re.IGNORECASE)
        add_qty = float(m.group(2))
        unit = (m.group(3) or None)
        # Foodness gate for add
        try:
            is_food_flag: bool | None = None
            # quick lexical whitelist; if not decisive, ask LLM classifier
            lex_hit = _lexicon_is_food_text(add_name)
            if lex_hit is True:
                is_food_flag = True
            else:
                is_food_flag = await _foodness_text(add_name)
        except Exception:
            is_food_flag = None
        if is_food_flag is False:
            return {"error": "not_food", "meta": {"action": action, "reason": "not_food"}}
        qty_g, _is_liq = _qty_to_grams(add_name, add_qty, unit)
        qty_g = max(1.0, min(1000.0, qty_g))
        add_cal, add_p, add_f, add_c = _estimate_from_name(add_name, qty_g)
        items = _clone_items(items_in)
        _item_out = {
            "name": add_name,
            "weight_g": qty_g,
            "calories": add_cal,
            "protein_g": add_p,
            "fat_g": add_f,
            "carbs_g": add_c,
        }
        # Preserve original unit for UI if user specified qty+unit
        try:
            if unit is not None:
                u = (str(unit) or "").lower()
                app = None
                if u in {"мл", "ml"}:
                    d = 1.0
                    key = (add_name or "").lower()
                    for k, val in densities.items():
                        if k in key:
                            d = val
                            break
                    app = {"unit": "ml", "qty": float(add_qty), "density": float(d), "approx_g": float(qty_g)}
                    _item_out["is_liquid"] = True
                elif u in {"л", "l"}:
                    app = {"unit": "l", "qty": float(add_qty), "density": 1.0, "approx_g": float(qty_g)}
                    _item_out["is_liquid"] = True
                elif u in {"шт", "pc", "pcs"}:
                    app = {"unit": "шт", "qty": float(add_qty), "approx_g": float(qty_g)}
                if app:
                    _item_out["appearance"] = app
        except Exception:
            pass
        try:
            # If converter told us it's liquid — mark it
            if unit is not None and '_is_liq' in locals() and bool(_is_liq):
                _item_out["is_liquid"] = True
        except Exception:
            pass
        items.append(_item_out)
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        # Prefer adding estimated delta to base calories to avoid undercount when base items were incomplete
        new_cal = int(max(out_cal, base_cal + add_cal))
        return {
            "title": (title or "Блюдо"),
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

    # add (fallback): no quantity specified → apply sensible defaults by category
    m = re.match(r"^\s*(?:добавить|добавь|положить|прибавить|\+)\s*([a-zа-яё\-\s]+?)\s*$", instr, flags=re.IGNORECASE)
    if m:
        action = "add"
        add_name = _normalize_name_ru((m.group(1) or "").strip().strip('- '))
        add_name = re.sub(r"^(?:добавить|добавь|положить|прибавить)\s+", "", add_name, flags=re.IGNORECASE)
        # Foodness gate (None -> allow)
        try:
            is_food_flag = _lexicon_is_food_text(add_name)
            if is_food_flag is not True:
                is_food_flag = await _foodness_text(add_name)
        except Exception:
            is_food_flag = None
        if is_food_flag is False:
            return {"error": "not_food", "meta": {"action": action, "reason": "not_food"}}
        # Defaults
        def _default_add_qty(name: str) -> tuple[float, str | None]:
            nl = (name or "").lower()
            if any(k in nl for k in ["петруш", "укроп", "базилик", "кинза", "руккол", "шпинат", "зелень"]):
                return 5.0, "г"
            if any(k in nl for k in ["соус", "кетчуп", "майонез", "сметан", "йогурт", "сливки"]):
                return 20.0, "г"
            if any(k in nl for k in ["масло", "оливковое масло"]):
                return 5.0, "г"
            return 30.0, "г"
        add_qty, unit = _default_add_qty(add_name)
        qty_g, _is_liq = _qty_to_grams(add_name, float(add_qty), unit)
        qty_g = max(1.0, min(1000.0, qty_g))
        add_cal, add_p, add_f, add_c = _estimate_from_name(add_name, qty_g)
        items = _clone_items(items_in)
        _item_out = {
            "name": add_name,
            "weight_g": qty_g,
            "calories": add_cal,
            "protein_g": add_p,
            "fat_g": add_f,
            "carbs_g": add_c,
        }
        items.append(_item_out)
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        new_cal = int(max(out_cal, base_cal + add_cal))
        return {
            "title": (title or "Блюдо"),
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
    m = re.search(r"(?:убрать|убери|удалить|удали|без|минус|\-)\s+([a-zа-яё\-\s]+)\b", instr, flags=re.IGNORECASE)
    if m:
        action = "remove"
        name = _normalize_name_ru((m.group(1) or "").strip())
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
        m1 = re.search(r"(увеличить|увеличь|уменьшить|уменьши)\s+порцию\s+на\s+(\d{1,3})%", t)
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
                old_w = float(it.get("weight_g") or 0)
                if it.get("weight_g") is not None:
                    it["weight_g"] = round(old_w * factor, 1)
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
            "calories": int(out_cal if out_cal > 0 else int(round(base_cal * factor))),
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
            "meta": {"action": action, "delta_cal": int((out_cal if out_cal > 0 else int(round(base_cal * factor))) - base_cal)},
        }

    # scale (times or percent) — fallback when NLU didn't trigger
    m = re.search(r"\b(увеличить|увеличь|уменьшить|уменьши|сократить|сократи)\s*(?:порцию\s*)?(?:в|x|х)\s*(\d+(?:[\.,]\d+)?)\s*раз[а]?\b", instr, flags=re.IGNORECASE)
    factor = None
    if m:
        verb = (m.group(1) or "").lower()
        n = float((m.group(2) or "1").replace(",", "."))
        factor = n if ("увелич" in verb) else (1.0 / n if n > 0 else 1.0)
    else:
        mp = re.search(r"\b(увеличить|увеличь|уменьшить|уменьши|сократить|сократи|больше|меньше)\b[^%\d]*([+\-−–]?\d{1,3})\s*%", instr, flags=re.IGNORECASE)
        if mp:
            verb = (mp.group(1) or "").lower()
            p = float((mp.group(2) or "0").replace("−", "-").replace("–", "-"))
            if p < 0:
                # explicit negative percentage like -30%
                factor = 1.0 + (p / 100.0)
            else:
                is_dec = any(k in verb for k in ["уменьш", "сократ", "меньш"])  # decrease
                factor = 1.0 - (p / 100.0) if is_dec else 1.0 + (p / 100.0)
    if factor is not None:
        try:
            factor = max(0.25, min(3.0, float(factor)))
        except Exception:
            factor = 1.0
        # Debug log
        try:
            logger.info("FoodAI:scale | path=regex | instr='{}' | factor_final={}", instr_raw, factor)
        except Exception:
            pass
        items = _clone_items(items_in)
        for it in items:
            try:
                old_w = float(it.get("weight_g") or 0)
                if it.get("weight_g") is not None:
                    it["weight_g"] = round(old_w * factor, 1)
                has_macros = (
                    it.get("calories") is not None and it.get("protein_g") is not None
                    and it.get("fat_g") is not None and it.get("carbs_g") is not None
                )
                if has_macros:
                    it["calories"] = int(round(float(it.get("calories") or 0) * factor))
                    it["protein_g"] = round(float(it.get("protein_g") or 0) * factor, 1)
                    it["fat_g"] = round(float(it.get("fat_g") or 0) * factor, 1)
                    it["carbs_g"] = round(float(it.get("carbs_g") or 0) * factor, 1)
                else:
                    n = str(it.get("name") or "")
                    w = float(it.get("weight_g") or 0)
                    cal, p, f, c = _estimate_from_name(n, w)
                    it.update({"calories": cal, "protein_g": p, "fat_g": f, "carbs_g": c})
            except Exception:
                continue
        out_cal, out_p, out_f, out_c, out_w = _sum_items(items)
        return {
            "title": title or "Блюдо",
            "calories": int(out_cal if out_cal > 0 else int(round(base_cal * factor))),
            "protein_g": float(out_p if out_p > 0 else round(base_p * factor, 1)),
            "fat_g": float(out_f if out_f > 0 else round(base_f * factor, 1)),
            "carbs_g": float(out_c if out_c > 0 else round(base_c * factor, 1)),
            "weight_g": float(round(out_w, 1)),
            "confidence": float((base or {}).get("confidence") or 0.8),
            "items": items,
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": None,
            "appearance": {},
            "not_food": False,
            "meta": {"action": "scale", "delta_cal": int((out_cal if out_cal > 0 else int(round(base_cal * factor))) - base_cal)},
        }

    # Fallback: unsupported
    return {"error": "unsupported_instruction", "meta": {"action": action, "reason": "unsupported"}}
