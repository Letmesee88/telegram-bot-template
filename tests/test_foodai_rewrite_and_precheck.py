import asyncio
import pytest

from bot.services import foodai as foodai_module
from bot.services.foodai import _compose_analysis_text, analyze_photo, refine_meal


@pytest.mark.asyncio
async def test_compose_analysis_text_rewrite_enforces_phrase_and_sanitizes(monkeypatch: pytest.MonkeyPatch):
    # Force OpenAI usage path inside _compose_analysis_text
    monkeypatch.setattr(foodai_module, "_use_openai", lambda: True)

    # Return text without MUST_PHRASE and with banned word in the first sentence
    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        return "похоже, это тарелка еды с чем-то. Остальной текст без особых ограничений."

    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    items = [
        {"name": "овсянка"},
        {"name": "банан"},
        {"name": "йогурт"},
    ]
    appearance = {"plate_visible": True, "plate_diameter_cm": 24, "is_packaged": False}

    txt = await _compose_analysis_text(items, appearance, confidence=0.82)
    assert isinstance(txt, str) and len(txt) > 0
    # First sentence must start with "На фото" due to sanitizer
    assert txt.startswith("На фото")
    # MUST phrase must be present after rewrite normalization
    assert "Использованы справочные данные ФИЦ питания и USDA." in txt
    # Length cap <= 420 chars
    assert len(txt) <= 420


@pytest.mark.asyncio
async def test_analyze_photo_precheck_strict_provider_unavailable(monkeypatch: pytest.MonkeyPatch):
    # Configure strict precheck
    foodai_module.settings.FOODAI_PRECHECK_STRICT = True

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    # Precheck fails twice -> None
    async def fake_foodness(url: str) -> bool | None:
        return None

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict)
    assert res.get("error") == "provider_unavailable"


@pytest.mark.asyncio
async def test_escalation_triggers_on_many_items_low_conf(monkeypatch: pytest.MonkeyPatch):
    # Escalation rule in code: escalate when there are many items (>= items_min)
    # AND confidence is below max(0.75, FOODAI_ESCALATE_CONF).
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = True
    foodai_module.settings.FOODAI_VISION_ESCALATION_CHAIN = "m1>m2"
    foodai_module.settings.FOODAI_VISION_MAX_STEPS = 2
    foodai_module.settings.FOODAI_ESCALATE_ITEMS_MIN = 3
    # Set confidence threshold to 0.7 (effective threshold will be 0.75)
    foodai_module.settings.FOODAI_ESCALATE_CONF = 0.7
    # Keep zero-fields off to isolate the many-items+low-conf rule
    foodai_module.settings.FOODAI_ESCALATE_ZERO_FIELDS = False

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        model = str(payload.get("model") or "")
        if model == "m1":
            # Many items (>=3) but low confidence (<0.75) -> should trigger escalation
            return (
                '{"title":"m1","calories":350,"protein_g":18,"fat_g":10,"carbs_g":40,'
                '"weight_g":320,'
                '"confidence":0.6,'
                '"items":['
                '{"name":"рис","calories":220,"protein_g":4,"fat_g":1.5,"carbs_g":48,"weight_g":200,"is_liquid":false},'
                '{"name":"курица","calories":220,"protein_g":23,"fat_g":12,"carbs_g":0,"weight_g":150,"is_liquid":false},'
                '{"name":"овощи","calories":40,"protein_g":2,"fat_g":0.2,"carbs_g":6,"weight_g":50,"is_liquid":false}'
                '],'
                '"references":{"sources":["ФГБУН \\\"ФИЦ питания и биотехнологии\\\"","USDA FoodData Central"]},'
                '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
                '"not_food":false}'
            )
        # m2 returns valid with >=3 items
        return (
            '{"title":"m2","calories":480,"protein_g":20,"fat_g":12,"carbs_g":55,'
            '"weight_g":380,"confidence":0.9,"items":[{"name":"рис","calories":220,'
            '"protein_g":4,"fat_g":1.5,"carbs_g":48,"weight_g":200,"is_liquid":false},'
            '{"name":"курица","calories":220,"protein_g":23,"fat_g":12,"carbs_g":0,"weight_g":150,"is_liquid":false},'
            '{"name":"овощи","calories":40,"protein_g":2,"fat_g":0.2,"carbs_g":6,"weight_g":50,"is_liquid":false}],'
            '"references":{"sources":["ФГБУН \\\"ФИЦ питания и биотехнологии\\\"","USDA FoodData Central"]},'
            '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict)
    assert res.get("title") == "m2"  # escalated to second model due to many_items+low_conf


@pytest.mark.asyncio
async def test_refine_meal_words_and_unicode_minus() -> None:
    base = {"title": "Блюдо", "calories": 300, "protein_g": 15.0, "fat_g": 10.0, "carbs_g": 30.0, "weight_g": 300.0, "items": [
        {"name": "рис", "weight_g": 200.0, "calories": 220, "protein_g": 4.0, "fat_g": 1.5, "carbs_g": 48.0},
    ]}

    # "вдвое" => factor ~ 2.0
    out1 = await refine_meal(base, "вдвое")
    assert not out1.get("error")
    assert out1.get("meta", {}).get("action") == "scale"
    assert float(out1.get("weight_g") or 0) >= 600.0 - 0.1

    # "наполовину" without explicit increase/decrease => reduce to 0.5x (see code logic)
    out2 = await refine_meal(base, "наполовину")
    assert not out2.get("error")
    assert float(out2.get("weight_g") or 0) <= 150.0 + 0.1

    # Unicode minus U+2212 in percentage: "−30%"
    out3 = await refine_meal(base, "−30%")
    assert not out3.get("error")
    assert 210.0 - 0.1 <= float(out3.get("weight_g") or 0) <= 210.0 + 0.1
