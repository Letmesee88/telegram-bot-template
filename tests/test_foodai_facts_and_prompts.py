import json
import pytest

from bot.services.foodai import analyze_photo
from bot.services import foodai as foodai_module


@pytest.mark.asyncio
async def test_analyze_photo_calls_visual_facts_and_passes_to_main(monkeypatch: pytest.MonkeyPatch):
    # Arrange settings for OpenAI + Responses + Facts
    foodai_module.settings.OPENAI_API_KEY = "test"
    foodai_module.settings.FOODAI_PROVIDER = "openai"
    foodai_module.settings.FOODAI_API = "responses"
    foodai_module.settings.FOODAI_USE_RESPONSES_FOR_5 = True
    foodai_module.settings.FOODAI_VISION_MODEL = "gpt-5-2025-08-07"
    foodai_module.settings.FOODAI_IMAGE_DETAIL = "high"
    foodai_module.settings.FOODAI_FACTS_ENABLED = True
    # Make API path deterministic for main call
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = False
    foodai_module.settings.FOODAI_ALLOW_FALLBACK_TO_4O_MINI = False
    foodai_module.settings.FOODAI_TEXT_FALLBACK_TO_CHAT = False
    # Disable analysis rewrite to keep only Facts + main calls
    foodai_module.settings.FOODAI_ANALYSIS_REWRITE = "off"

    # Monkeypatch helpers
    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    calls: list[tuple[str, dict]] = []

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        calls.append((kind, payload))
        # First Responses call should be Visual Facts
        if len(calls) == 1:
            assert kind == "responses"
            instr = payload.get("instructions") or ""
            assert "Extract observable visual facts" in instr
            return json.dumps({
                "container": {"type": "plate", "fill_fraction": 0.6},
                "base_present": "none",
                "unit_items": [{"kind": "falafel", "count": 3}],
                "confidence_facts": 0.8,
            })
        # Second call is the main analysis (may be 'responses' or 'chat' depending on settings)
        assert kind in {"responses", "chat"}
        # Ensure Visual Facts are injected into input_text
        contents = (payload.get("input") or [{}])[0].get("content") or []
        texts = [c.get("text") for c in contents if c.get("type") in {"input_text", "text"}]
        assert any(isinstance(t, str) and "Visual Facts:" in t for t in texts)
        # Return a valid JSON matching schema
        data = {
            "title": "салат с фалафелем",
            "calories": 320,
            "protein_g": 12.0,
            "fat_g": 14.0,
            "carbs_g": 34.0,
            "weight_g": 300.0,
            "confidence": 0.82,
            "items": [
                {
                    "name": "свежие овощи (помидоры, огурцы)",
                    "calories": 60,
                    "protein_g": 2.0,
                    "fat_g": 1.0,
                    "carbs_g": 10.0,
                    "weight_g": 100.0,
                    "is_liquid": False,
                },
                {
                    "name": "фалафель",
                    "calories": 180,
                    "protein_g": 8.0,
                    "fat_g": 8.0,
                    "carbs_g": 16.0,
                    "weight_g": 120.0,
                    "is_liquid": False,
                },
                {
                    "name": "соус",
                    "calories": 80,
                    "protein_g": 2.0,
                    "fat_g": 5.0,
                    "carbs_g": 8.0,
                    "weight_g": 80.0,
                    "is_liquid": False,
                },
            ],
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": "На фото салат с фалафелем. Вес посуды не учитывался. Использованы справочные данные ФИЦ питания и USDA.",
            "appearance": {"is_packaged": False, "plate_visible": True, "plate_diameter_cm": 24},
            "not_food": False,
        }
        return json.dumps(data)

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    # Act
    res = await analyze_photo("fake_photo_id")

    # Assert
    assert isinstance(res, dict)
    assert res.get("error") is None
    assert float(res.get("weight_g") or 0) > 0
    assert (res.get("references") or {}).get("sources") == [
        "ФГБУН \"ФИЦ питания и биотехнологии\"",
        "USDA FoodData Central",
    ]
    # Ensure Visual Facts + main analysis were called (rewrite may add an extra call)
    assert len(calls) >= 2


@pytest.mark.asyncio
async def test_analyze_photo_high_detail_retry_without_escalation(monkeypatch: pytest.MonkeyPatch):
    # Arrange B + D config: no escalation/chat, use Responses, enable Facts
    foodai_module.settings.OPENAI_API_KEY = "test"
    foodai_module.settings.FOODAI_PROVIDER = "openai"
    foodai_module.settings.FOODAI_API = "responses"
    foodai_module.settings.FOODAI_USE_RESPONSES_FOR_5 = True
    foodai_module.settings.FOODAI_VISION_MODEL = "gpt-5-2025-08-07"
    foodai_module.settings.FOODAI_FACTS_ENABLED = True
    # Force initial detail low to trigger high retry when confidence is low
    foodai_module.settings.FOODAI_IMAGE_DETAIL = "low"
    foodai_module.settings.FOODAI_IMAGE_DETAIL_HIGH_RETRY = True
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = False
    foodai_module.settings.FOODAI_ALLOW_FALLBACK_TO_4O_MINI = False
    foodai_module.settings.FOODAI_TEXT_FALLBACK_TO_CHAT = False
    # Disable analysis rewrite to keep only Facts + main(low) + main(high)
    foodai_module.settings.FOODAI_ANALYSIS_REWRITE = "off"

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    calls: list[tuple[str, dict]] = []

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        calls.append((kind, payload))
        idx = len([1 for k, _ in calls if k == "responses"])  # count responses calls so far
        if idx == 1:
            # Visual Facts
            return json.dumps({"container": {"type": "bowl", "fill_fraction": 0.7}})
        # Main analysis responses calls: first low-confidence, then high-confidence
        contents = (payload.get("input") or [{}])[0].get("content") or []
        imgs = [c for c in contents if c.get("type") == "input_image"]
        assert imgs, "input_image missing in payload"
        detail = imgs[0].get("detail")
        # First main call should be with low detail and low confidence to trigger retry
        if idx == 2:
            assert detail in {"low", "auto"}
            return json.dumps({
                "title": "боул",
                "calories": 350,
                "protein_g": 12.0,
                "fat_g": 10.0,
                "carbs_g": 50.0,
                "weight_g": 320.0,
                "confidence": 0.5,  # low -> triggers high retry
                "items": [],
                "references": {"sources": [
                    "ФГБУН \"ФИЦ питания и биотехнологии\"",
                    "USDA FoodData Central",
                ]},
                "analysis_text": None,
                "appearance": {"is_packaged": False, "plate_visible": False, "plate_diameter_cm": None},
                "not_food": False,
            })
        # Second main call should be with high detail and higher confidence
        assert detail == "high"
        return json.dumps({
            "title": "боул",
            "calories": 360,
            "protein_g": 13.0,
            "fat_g": 11.0,
            "carbs_g": 51.0,
            "weight_g": 330.0,
            "confidence": 0.9,
            "items": [
                {"name": "рис", "calories": 220, "protein_g": 4.0, "fat_g": 1.0, "carbs_g": 48.0, "weight_g": 200.0, "is_liquid": False}
            ],
            "references": {"sources": [
                "ФГБУН \"ФИЦ питания и биотехнологии\"",
                "USDA FoodData Central",
            ]},
            "analysis_text": "На фото боул. Вес посуды не учитывался. Использованы справочные данные ФИЦ питания и USDA.",
            "appearance": {"is_packaged": False, "plate_visible": True, "plate_diameter_cm": 24},
            "not_food": False,
        })

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict)
    assert res.get("error") is None
    assert float(res.get("confidence") or 0) >= 0.7
    # Visual Facts + 2 main calls expected
    resp_calls = [1 for k, _ in calls if k == "responses"]
    assert len(resp_calls) == 3
