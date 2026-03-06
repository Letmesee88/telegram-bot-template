import json

import pytest

from bot.services import foodai as foodai_module
from bot.services.foodai import analyze_photo


@pytest.mark.asyncio
async def test_analyze_photo_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        # Always return a valid structured output for the schema
        data = {
            "title": "овсянка с бананом",
            "calories": 380,
            "protein_g": 14.0,
            "fat_g": 9.0,
            "carbs_g": 60.0,
            "weight_g": 420.0,
            "confidence": 0.85,
            "items": [
                {
                    "name": "овсянка",
                    "calories": 250,
                    "protein_g": 9.0,
                    "fat_g": 5.0,
                    "carbs_g": 42.0,
                    "weight_g": 300.0,
                    "is_liquid": False,
                }
            ],
            "references": {
                "sources": [
                    'ФГБУН "ФИЦ питания и биотехнологии"',
                    "USDA FoodData Central",
                ]
            },
            "analysis_text": "На фото овсянка и банан. Использованы справочные данные ФИЦ питания и USDA.",
            "appearance": {"is_packaged": False, "plate_visible": True, "plate_diameter_cm": 24},
            "not_food": False,
        }
        return json.dumps(data)

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict)
    assert not res.get("error")
    assert (res.get("title") or "").strip() != ""
    assert isinstance(res.get("items"), list)
    refs = (res.get("references") or {}).get("sources")
    assert refs == [
        'ФГБУН "ФИЦ питания и биотехнологии"',
        "USDA FoodData Central",
    ]


@pytest.mark.asyncio
async def test_analyze_photo_not_food(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return False

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict)
    assert res.get("not_food") is True
    # Use default in dict.get to avoid treating 0 as falsy
    assert int(res.get("calories", -1)) == 0
    assert (res.get("items") or []) == []


@pytest.mark.asyncio
async def test_analyze_photo_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange settings to use a simple chain m1>m2 and force at most 2 steps
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = True
    foodai_module.settings.FOODAI_VISION_ESCALATION_CHAIN = "m1>m2"
    foodai_module.settings.FOODAI_VISION_MAX_STEPS = 2
    foodai_module.settings.FOODAI_VISION_DETAIL_ORDER = "low"  # single step over detail

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        model = str(payload.get("model") or "")
        if model == "m1":
            # Deliberately weak result: low confidence and zero macros to trigger escalation
            return json.dumps({
                "title": "слабый результат m1",
                "calories": 0,
                "protein_g": 0.0,
                "fat_g": 0.0,
                "carbs_g": 0.0,
                "weight_g": 0.0,
                "confidence": 0.5,
                "items": [],
                "references": {"sources": ['ФГБУН "ФИЦ питания и биотехнологии"', "USDA FoodData Central"]},
                "analysis_text": None,
                "appearance": {"is_packaged": False, "plate_visible": False, "plate_diameter_cm": None},
                "not_food": False,
            })
        # m2 returns a solid result
        return json.dumps({
            "title": "результат m2",
            "calories": 420,
            "protein_g": 18.0,
            "fat_g": 12.0,
            "carbs_g": 50.0,
            "weight_g": 380.0,
            "confidence": 0.9,
            "items": [
                {"name": "рис", "calories": 220, "protein_g": 4.0, "fat_g": 1.5, "carbs_g": 48.0, "weight_g": 200.0, "is_liquid": False}
            ],
            "references": {"sources": ['ФГБУН "ФИЦ питания и биотехнологии"', "USDA FoodData Central"]},
            "analysis_text": "На фото рис. Использованы справочные данные ФИЦ питания и USDA.",
            "appearance": {"is_packaged": False, "plate_visible": True, "plate_diameter_cm": 24},
            "not_food": False,
        })

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict)
    # Ensure we landed on the strong result from m2
    assert res.get("title") == "результат m2"
    assert float(res.get("confidence") or 0) >= 0.75
    refs = (res.get("references") or {}).get("sources")
    assert refs == [
        'ФГБУН "ФИЦ питания и биотехнологии"',
        "USDA FoodData Central",
    ]
