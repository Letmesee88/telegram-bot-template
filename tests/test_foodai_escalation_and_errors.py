import pytest

from bot.services import foodai as foodai_module
from bot.services.foodai import analyze_photo


@pytest.mark.asyncio
async def test_escalation_low_conf_without_many_items(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup: low confidence (< threshold) should trigger escalation even with few items
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = True
    foodai_module.settings.FOODAI_VISION_ESCALATION_CHAIN = "m1>m2"
    foodai_module.settings.FOODAI_VISION_DETAIL_ORDER = "low"
    foodai_module.settings.FOODAI_VISION_MAX_STEPS = 2
    foodai_module.settings.FOODAI_ESCALATE_CONF = 0.7
    foodai_module.settings.FOODAI_ESCALATE_ZERO_FIELDS = False

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        model = str(payload.get("model") or "")
        if model == "m1":
            return (
                '{"title":"m1","calories":300,"protein_g":15,"fat_g":10,"carbs_g":35,'
                '"weight_g":300,"confidence":0.6,'
                '"items":[{"name":"рис","calories":200,"protein_g":4,"fat_g":1,'
                '"carbs_g":45,"weight_g":180,"is_liquid":false}],'
                '"references":{"sources":["ФГБУН \\"ФИЦ питания и биотехнологии\\"","USDA FoodData Central"]},'
                '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
                '"not_food":false}'
            )
        return (
            '{"title":"m2","calories":450,"protein_g":20,"fat_g":12,"carbs_g":55,'
            '"weight_g":380,"confidence":0.9,'
            '"items":[{"name":"рис","calories":220,"protein_g":4,"fat_g":1.5,"carbs_g":48,"weight_g":200,"is_liquid":false}],'
            '"references":{"sources":["ФГБУН \\"ФИЦ питания и биотехнологии\\"","USDA FoodData Central"]},'
            '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert res.get("title") == "m2"  # escalated due to low confidence


@pytest.mark.asyncio
async def test_escalation_zero_fields_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup: zero_fields enabled, any zero in macros triggers escalation
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = True
    foodai_module.settings.FOODAI_VISION_ESCALATION_CHAIN = "m1>m2"
    foodai_module.settings.FOODAI_VISION_DETAIL_ORDER = "low"
    foodai_module.settings.FOODAI_VISION_MAX_STEPS = 2
    foodai_module.settings.FOODAI_ESCALATE_ZERO_FIELDS = True
    foodai_module.settings.FOODAI_ESCALATE_CONF = 0.0  # ignore confidence

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        model = str(payload.get("model") or "")
        if model == "m1":
            # Zero in macros (fat_g=0) -> should be weak by zero_fields
            return (
                '{"title":"m1","calories":300,"protein_g":15,"fat_g":0,"carbs_g":35,'
                '"weight_g":300,"confidence":0.95,'
                '"items":[{"name":"рис","calories":200,"protein_g":4,"fat_g":1,'
                '"carbs_g":45,"weight_g":180,"is_liquid":false}],'
                '"references":{"sources":["ФГБУН \\"ФИЦ питания и биотехнологии\\"","USDA FoodData Central"]},'
                '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
                '"not_food":false}'
            )
        return (
            '{"title":"m2","calories":450,"protein_g":20,"fat_g":12,"carbs_g":55,'
            '"weight_g":380,"confidence":0.9,'
            '"items":[{"name":"рис","calories":220,"protein_g":4,"fat_g":1.5,"carbs_g":48,"weight_g":200,"is_liquid":false}],'
            '"references":{"sources":["ФГБУН \\"ФИЦ питания и биотехнологии\\"","USDA FoodData Central"]},'
            '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert res.get("title") == "m2"  # escalated due to zero_fields


@pytest.mark.asyncio
async def test_provider_error_first_model_then_second_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # First model returns None (HTTP error/timeout), second returns valid JSON
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = True
    foodai_module.settings.FOODAI_VISION_ESCALATION_CHAIN = "m1>m2"
    foodai_module.settings.FOODAI_VISION_DETAIL_ORDER = "low"
    foodai_module.settings.FOODAI_VISION_MAX_STEPS = 2

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        model = str(payload.get("model") or "")
        if model == "m1":
            return None  # emulate HTTP error/timeout
        return (
            '{"title":"m2","calories":480,"protein_g":20,"fat_g":12,"carbs_g":55,'
            '"weight_g":380,"confidence":0.9,"items":[{"name":"рис","calories":220,'
            '"protein_g":4,"fat_g":1.5,"carbs_g":48,"weight_g":200,"is_liquid":false}],'
            '"references":{"sources":["ФГБУН \\"ФИЦ питания и биотехнологии\\"","USDA FoodData Central"]},'
            '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert res.get("title") == "m2"  # graceful recovery via next model


@pytest.mark.asyncio
async def test_provider_all_fail_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # All attempts return None -> provider_unavailable
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = True
    foodai_module.settings.FOODAI_VISION_ESCALATION_CHAIN = "m1>m2"
    foodai_module.settings.FOODAI_VISION_DETAIL_ORDER = "low"
    foodai_module.settings.FOODAI_VISION_MAX_STEPS = 2

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        return None

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    res = await analyze_photo("fake_photo_id")
    assert res.get("error") == "provider_unavailable"
