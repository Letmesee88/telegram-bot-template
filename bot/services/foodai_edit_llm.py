from __future__ import annotations
import contextlib
import json
from time import perf_counter
from typing import Any

from aiohttp import ClientSession

from bot.core.config import settings
from bot.metrics import (
    foodai_edit_nlu_duration_ms,
    foodai_edit_nlu_failed,
    foodai_edit_nlu_started,
    foodai_edit_nlu_succeeded,
)

# Strict JSON schema (informal, validated in code)
_ALLOWED_ACTIONS = {"add", "remove", "replace", "scale", "change_qty"}
_ALLOWED_UNITS = {None, "g", "гр", "грамм", "ml", "мл", "l", "л", "pcs", "шт"}


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
                    return None
                return await r.json()
    except Exception:
        return None


def _build_system_prompt() -> str:
    return (
        "Ты интерпретатор команд редактирования блюда. Верни строго JSON без пояснений.\n"
        "Схема: {\n"
        "  action: one of add|remove|replace|scale|change_qty,\n"
        "  target: string|null,\n"
        "  replacement: string|null,\n"
        "  qty_g: number|null,\n"
        "  unit: g|гр|грамм|ml|мл|l|л|pcs|шт|null,\n"
        "  factor: number|null,\n"
        "  confidence: number 0..1\n"
        "}. Ничего не выдумывай, калории не считай. Если не уверен — выбери action=remove/add/replace/scale/change_qty наиболее подходящий.\n"
        "Если речь про изменение порции — используй action=scale и factor (например 2.0).\n"
        "Если речь про изменение веса ингредиента — action=change_qty + target + qty_g + unit.\n"
        "Если речь про удаление — action=remove + target.\n"
        "Добавление — action=add + target + qty_g + unit.\n"
        "Замена — action=replace + target + replacement (+ qty_g/unit если указано).\n"
        "Верни ТОЛЬКО JSON."
    )


def _build_user_prompt(base: dict[str, Any], instruction: str) -> str:
    # Truncate items to names/weights to keep prompt small
    items = base.get("items") or []
    items_min = [{"name": str(it.get("name", "")), "weight_g": it.get("weight_g")} for it in items][:20]
    b = {
        "title": base.get("title"),
        "weight_g": base.get("weight_g"),
        "items": items_min,
    }
    return (
        "BASE:\n" + json.dumps(b, ensure_ascii=False) + "\n\n"
        "INSTRUCTION:\n" + instruction.strip()
    )


def _coerce_unit(u: str | None) -> str | None:
    if u is None:
        return None
    u = str(u).lower().strip()
    if u in {"гр", "грамм"}:
        return "g"
    if u in {"мл"}:
        return "ml"
    if u == "л":
        return "l"
    if u == "шт":
        return "pcs"
    if u in {"g", "ml", "l", "pcs"}:
        return u
    return None


def _validate_result(obj: Any) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if not isinstance(obj, dict):
            return None, "not_dict"
        action = obj.get("action")
        if action not in _ALLOWED_ACTIONS:
            return None, "bad_action"
        target = obj.get("target")
        replacement = obj.get("replacement")
        qty_g = obj.get("qty_g")
        unit = _coerce_unit(obj.get("unit"))
        factor = obj.get("factor")
        conf = obj.get("confidence")
        if unit not in _ALLOWED_UNITS:
            return None, "bad_unit"
        if qty_g is not None:
            try:
                qty_g = float(qty_g)
            except Exception:
                return None, "bad_qty"
            qty_g = max(1.0, min(1000.0, qty_g))
        if factor is not None:
            try:
                factor = float(factor)
            except Exception:
                return None, "bad_factor"
            factor = max(0.25, min(3.0, factor))
        out = {
            "action": action,
            "target": (str(target) if target is not None else None),
            "replacement": (str(replacement) if replacement is not None else None),
            "qty_g": qty_g,
            "unit": unit,
            "factor": factor,
            "confidence": float(conf) if conf is not None else None,
        }
        # basic consistency
        if action == "remove" and not out["target"]:
            return None, "remove_no_target"
        if action == "add" and (not out["target"] or out["qty_g"] is None):
            return None, "add_missing"
        if action == "replace" and (not out["target"] or not out["replacement"]):
            return None, "replace_missing"
        if action == "change_qty" and (not out["target"] or out["qty_g"] is None):
            return None, "change_qty_missing"
        if action == "scale" and out["factor"] is None:
            return None, "scale_missing"
        return out, None
    except Exception:
        return None, "validate_error"


async def interpret_edit(base: dict[str, Any], instruction: str) -> dict[str, Any] | dict[str, str]:
    """Return structured edit command or {"error": reason}."""
    if not settings.OPENAI_API_KEY:
        return {"error": "no_api_key"}

    t0 = perf_counter()
    with contextlib.suppress(Exception):
        foodai_edit_nlu_started.inc()

    payload = {
        "model": settings.FOODAI_EDIT_MODEL or settings.FOODAI_DEFAULT_MODEL,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": _build_user_prompt(base, instruction)},
        ],
        "response_format": {"type": "json_object"},
    }
    data = await _openai_chat(payload)
    if not data:
        with contextlib.suppress(Exception):
            foodai_edit_nlu_failed.labels(reason="provider_unavailable").inc()
        return {"error": "provider_unavailable"}

    try:
        content = (
            data["choices"][0]["message"]["content"]
            if data and "choices" in data and data["choices"]
            else ""
        )
        obj = json.loads(content)
    except Exception:
        with contextlib.suppress(Exception):
            foodai_edit_nlu_failed.labels(reason="parse_json").inc()
        return {"error": "parse_json"}

    obj, err = _validate_result(obj)
    if err:
        with contextlib.suppress(Exception):
            foodai_edit_nlu_failed.labels(reason=err).inc()
        return {"error": err}

    try:
        foodai_edit_nlu_succeeded.inc()
        foodai_edit_nlu_duration_ms.observe(max(0.0, (perf_counter() - t0) * 1000.0))
    except Exception:
        pass
    return obj  # type: ignore
