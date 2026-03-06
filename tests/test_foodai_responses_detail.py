from __future__ import annotations
import json

import pytest

import bot.services.foodai as svc


@pytest.mark.asyncio
async def test_responses_payload_includes_detail_high(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force settings for this test
    monkeypatch.setattr(svc.settings, "FOODAI_USE_RESPONSES_FOR_5", True, raising=False)
    monkeypatch.setattr(svc.settings, "FOODAI_IMAGE_DETAIL", "high", raising=False)
    monkeypatch.setattr(svc.settings, "FOODAI_VISION_DETAIL_ORDER", "high", raising=False)
    monkeypatch.setattr(svc.settings, "FOODAI_VISION_ESCALATION_ENABLED", False, raising=False)
    monkeypatch.setattr(svc.settings, "FOODAI_VISION_MAX_STEPS", 1, raising=False)
    monkeypatch.setattr(svc.settings, "FOODAI_VISION_MODEL", "gpt-5-mini-2025-08-07", raising=False)

    # Stub Telegram file resolving (async)
    async def _fake_tg_file_url(fid: str) -> str:
        return "https://example.com/file.jpg"

    # Precheck says it's food (async)
    async def _fake_foodness_photo(url: str) -> bool:
        return True

    monkeypatch.setattr(svc, "_tg_file_url", _fake_tg_file_url, raising=False)
    monkeypatch.setattr(svc, "_foodness_photo", _fake_foodness_photo, raising=False)

    # Capture payload passed to _openai_request
    captured: dict | None = None

    async def _fake_openai_request(kind: str, payload: dict):
        nonlocal captured
        # Only capture the main analysis payload (schema name 'foodai_result')
        if kind == "responses":
            try:
                fmt = (((payload or {}).get("text") or {}).get("format") or {})
                name = fmt.get("name") if isinstance(fmt, dict) else None
            except Exception:
                name = None
            if name == "foodai_result":
                captured = payload
                return json.dumps({
                    "title": "Тест блюдо",
                    "calories": 100,
                    "protein_g": 10.0,
                    "fat_g": 5.0,
                    "carbs_g": 12.0,
                    "weight_g": 150.0,
                    "confidence": 0.8,
                    "items": [],
                    "references": {"sources": ['ФГБУН "ФИЦ питания и биотехнологии"', "USDA FoodData Central"]},
                    "analysis_text": "Короткий анализ. Использованы справочные данные ФИЦ питания и USDA.",
                    "appearance": {"is_packaged": False, "plate_visible": False, "plate_diameter_cm": None},
                    "not_food": False,
                })
            if name == "foodness":
                return json.dumps({"is_food": True})
        # For other calls (e.g., rewrite or chat), return a benign text/None
        return "На фото блюдо. Использованы справочные данные ФИЦ питания и USDA."

    monkeypatch.setattr(svc, "_openai_request", _fake_openai_request, raising=False)

    # Run analyze
    out = await svc.analyze_photo("abc")
    assert isinstance(out, dict)
    assert out.get("calories") == 100

    # Validate captured payload
    assert captured is not None
    assert captured.get("model") == "gpt-5-mini-2025-08-07"
    # Find the input_image part
    content = captured["input"][0]["content"]
    img_parts = [p for p in content if p.get("type") == "input_image"]
    assert img_parts, "input_image part not found in Responses payload"
    assert img_parts[0].get("detail") == "high", "detail=high must be present in input_image payload"
