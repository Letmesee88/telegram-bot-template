import json
import pytest

from bot.services import foodai as foodai_module
from bot.services.foodai import analyze_photo, refine_meal


# ===== Rewrite modes =====
@pytest.mark.asyncio
async def test_rewrite_always_triggers_and_enforces_rules(monkeypatch: pytest.MonkeyPatch):
    # Arrange
    foodai_module.settings.FOODAI_ANALYSIS_REWRITE = "always"
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = False

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    # Source with banned opener and without MUST phrase
    def make_payload_resp():
        return (
            '{"title":"ok","calories":350,"protein_g":18,"fat_g":10,"carbs_g":40,'
            '"weight_g":320,"confidence":0.9,'
            '"items":[{"name":"овсянка","calories":180,"protein_g":6,"fat_g":3,"carbs_g":30,"weight_g":150,"is_liquid":false},'
            '{"name":"банан","calories":90,"protein_g":1.1,"fat_g":0.3,"carbs_g":22,"weight_g":100,"is_liquid":false}],'
            '"references":{"sources":["ФГБУН \\\"ФИЦ питания и биотехнологии\\\"","USDA FoodData Central"]},'
            '"analysis_text":"похоже, это миска овсянки и банана. Без обязательной фразы.",'
            '"appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    # Count compose calls
    called = {"cnt": 0}

    async def fake_compose(items, appearance, confidence):
        called["cnt"] += 1
        # Valid rewritten text that satisfies all constraints
        return (
            "На фото домашнее блюдо: овсянка и банан; оценка объёма по тарелке ~24 см и количеству ингредиентов. "
            "Порция средняя, баланс макроэлементов умеренный. Использованы справочные данные ФИЦ питания и USDA."
        )

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        return make_payload_resp()

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)
    monkeypatch.setattr(foodai_module, "_compose_analysis_text", fake_compose)

    # Act
    res = await analyze_photo("fake_photo_id")

    # Assert
    assert isinstance(res, dict) and not res.get("error")
    txt = str(res.get("analysis_text") or "")
    assert called["cnt"] == 1
    assert txt.startswith("На фото")
    assert txt.count("Использованы справочные данные ФИЦ питания и USDA.") == 1
    assert len(txt) <= 420
    assert not any(w in txt.split(".")[0].lower() for w in ["выгляд", "похож"])  # no cliché in opener


@pytest.mark.asyncio
async def test_rewrite_off_does_not_trigger_compose_but_sanitizes(monkeypatch: pytest.MonkeyPatch):
    foodai_module.settings.FOODAI_ANALYSIS_REWRITE = "off"
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = False

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        return (
            '{"title":"ok","calories":350,"protein_g":18,"fat_g":10,"carbs_g":40,'
            '"weight_g":320,"confidence":0.9,'
            '"items":[{"name":"рис","calories":180,"protein_g":4,"fat_g":1,"carbs_g":38,"weight_g":150,"is_liquid":false},'
            '{"name":"овощи","calories":40,"protein_g":2,"fat_g":0.2,"carbs_g":6,"weight_g":50,"is_liquid":false}],'
            '"references":{"sources":["ФГБУН \\\"ФИЦ питания и биотехнологии\\\"","USDA FoodData Central"]},'
            '"analysis_text":"выглядит как что-то с рисом. Без обязательной фразы.",'
            '"appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    async def fake_compose(*args, **kwargs):  # must NOT be called
        raise AssertionError("_compose_analysis_text must not be called when rewrite=off")

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)
    monkeypatch.setattr(foodai_module, "_compose_analysis_text", fake_compose)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict) and not res.get("error")
    txt = str(res.get("analysis_text") or "")
    assert txt.startswith("На фото")  # opener sanitized even without rewrite
    # MUST phrase may be absent in off mode
    assert txt.count("Использованы справочные данные ФИЦ питания и USDA.") in (0, 1)


@pytest.mark.asyncio
async def test_rewrite_auto_uses_needs_rewrite(monkeypatch: pytest.MonkeyPatch):
    foodai_module.settings.FOODAI_ANALYSIS_REWRITE = "auto"
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = False

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        return (
            '{"title":"ok","calories":350,"protein_g":18,"fat_g":10,"carbs_g":40,'
            '"weight_g":320,"confidence":0.9,'
            '"items":[{"name":"овсянка","calories":180,"protein_g":6,"fat_g":3,"carbs_g":30,"weight_g":150,"is_liquid":false},'
            '{"name":"банан","calories":90,"protein_g":1.1,"fat_g":0.3,"carbs_g":22,"weight_g":100,"is_liquid":false}],'
            '"references":{"sources":["ФГБУН \\\"ФИЦ питания и биотехнологии\\\"","USDA FoodData Central"]},'
            '"analysis_text":"похоже, это миска овсянки и банана.",'
            '"appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    called = {"needs": 0, "compose": 0}

    def fake_needs_rewrite(analysis_text, items):
        called["needs"] += 1
        return True, "length"

    async def fake_compose(items, appearance, confidence):
        called["compose"] += 1
        return (
            "На фото домашнее блюдо с овсянкой и бананом; оценка по тарелке ~24 см и объёму. "
            "Использованы справочные данные ФИЦ питания и USDA."
        )

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)
    monkeypatch.setattr(foodai_module, "_needs_rewrite", fake_needs_rewrite)
    monkeypatch.setattr(foodai_module, "_compose_analysis_text", fake_compose)

    res = await analyze_photo("fake_photo_id")
    assert isinstance(res, dict) and not res.get("error")
    assert called["needs"] >= 1
    assert called["compose"] == 1
    txt = str(res.get("analysis_text") or "")
    assert txt.count("Использованы справочные данные ФИЦ питания и USDA.") == 1
    assert len(txt) <= 420


# ===== refine_meal units and operations =====
@pytest.mark.asyncio
async def test_refine_meal_change_qty_ml_and_pcs(monkeypatch: pytest.MonkeyPatch):
    base = {
        "title": "Блюдо",
        "calories": 300,
        "protein_g": 15.0,
        "fat_g": 10.0,
        "carbs_g": 30.0,
        "weight_g": 300.0,
        "items": [
            {"name": "масло", "weight_g": 10.0},
            {"name": "кефир", "weight_g": 200.0},
            {"name": "яйцо", "weight_g": 60.0},
        ],
    }

    # 1) масло 5 мл -> density ~0.91 -> ~4.55 g, is_liquid and appearance
    out1 = await refine_meal(base, "масло 5 мл")
    assert not out1.get("error")
    it1 = next((i for i in out1.get("items", []) if i.get("name", "").lower().startswith("масло")), None)
    assert it1 is not None
    assert 4.0 <= float(it1.get("weight_g") or 0) <= 6.0
    app1 = it1.get("appearance")
    assert app1 and app1.get("unit") == "ml" and app1.get("qty") == 5.0
    assert it1.get("is_liquid") is True

    # 2) яйцо 2 шт -> ~100 g
    out2 = await refine_meal(base, "яйцо 2 шт")
    assert not out2.get("error")
    it2 = next((i for i in out2.get("items", []) if i.get("name", "").lower().startswith("яйц")), None)
    assert it2 is not None
    assert 95.0 <= float(it2.get("weight_g") or 0) <= 105.0
    app2 = it2.get("appearance")
    assert app2 and app2.get("unit") == "шт" and app2.get("qty") == 2.0


@pytest.mark.asyncio
async def test_refine_meal_change_qty_liters_via_nlu(monkeypatch: pytest.MonkeyPatch):
    # Enable NLU and inject interpreter to allow decimal liters
    foodai_module.settings.FOODAI_EDIT_NLU = True
    foodai_module.settings.OPENAI_API_KEY = "test"

    async def fake_interpret_edit(base_for_nlu, instr_raw):
        assert "кефир" in instr_raw.lower()
        return {"action": "change_qty", "target": "кефир", "qty_g": 0.2, "unit": "л"}

    monkeypatch.setattr(foodai_module, "interpret_edit", fake_interpret_edit)

    base = {
        "title": "Блюдо",
        "calories": 300,
        "protein_g": 15.0,
        "fat_g": 10.0,
        "carbs_g": 30.0,
        "weight_g": 300.0,
        "items": [
            {"name": "кефир", "weight_g": 100.0},
        ],
    }

    out = await refine_meal(base, "кефир 0.2 л")
    assert not out.get("error")
    it = next((i for i in out.get("items", []) if i.get("name", "").lower().startswith("кефир")), None)
    assert it is not None
    # 0.2 l -> ~200 ml -> density 1.03 -> ~206 g
    assert 195.0 <= float(it.get("weight_g") or 0) <= 215.0
    assert it.get("is_liquid") is True
    app = it.get("appearance")
    assert app and app.get("unit") == "l" and app.get("qty") == 0.2


@pytest.mark.asyncio
async def test_refine_meal_replace_with_qty_unit_and_remove(monkeypatch: pytest.MonkeyPatch):
    base = {
        "title": "Блюдо",
        "calories": 300,
        "protein_g": 15.0,
        "fat_g": 10.0,
        "carbs_g": 30.0,
        "weight_g": 300.0,
        "items": [
            {"name": "сыр", "weight_g": 50.0},
            {"name": "рис", "weight_g": 200.0},
        ],
    }

    # replace сыр -> молоко 200 мл
    out1 = await refine_meal(base, "заменить сыр на молоко 200 мл")
    assert not out1.get("error")
    it1 = next((i for i in out1.get("items", []) if i.get("name", "").lower().startswith("молок")), None)
    assert it1 is not None
    assert it1.get("is_liquid") is True
    app1 = it1.get("appearance")
    assert app1 and app1.get("unit") in ("ml", "l") and app1.get("qty") == 200.0

    # remove рис
    out2 = await refine_meal(base, "убрать рис")
    assert not out2.get("error")
    names2 = [i.get("name") for i in out2.get("items", [])]
    assert not any("рис" in (n or "").lower() for n in names2)


# ===== API path selection for gpt-5 =====
@pytest.mark.asyncio
async def test_api_path_selection_for_gpt5_responses_vs_chat(monkeypatch: pytest.MonkeyPatch):
    # Force single-model path and disable escalation
    foodai_module.settings.FOODAI_VISION_ESCALATION_ENABLED = False
    foodai_module.settings.FOODAI_ANALYSIS_REWRITE = "off"
    foodai_module.settings.FOODAI_IMAGE_DETAIL_HIGH_RETRY = False
    foodai_module.settings.FOODAI_ALLOW_FALLBACK_TO_4O_MINI = False
    foodai_module.settings.FOODAI_TEXT_FALLBACK_TO_CHAT = False
    foodai_module.settings.FOODAI_FACTS_ENABLED = True

    async def fake_tg_file_url(file_id: str) -> str | None:
        return "https://example.com/img.jpg"

    async def fake_foodness(url: str) -> bool | None:
        return True

    calls = []

    async def fake_openai_request(kind: str, payload: dict) -> str | None:
        calls.append(kind)
        return (
            '{"title":"ok","calories":350,"protein_g":18,"fat_g":10,"carbs_g":40,'
            '"weight_g":320,"confidence":0.9,'
            '"items":[],"references":{"sources":["ФГБУН \\\"ФИЦ питания и биотехнологии\\\"","USDA FoodData Central"]},'
            '"analysis_text":"ok","appearance":{"is_packaged":false,"plate_visible":true,"plate_diameter_cm":24},'
            '"not_food":false}'
        )

    monkeypatch.setattr(foodai_module, "_tg_file_url", fake_tg_file_url)
    monkeypatch.setattr(foodai_module, "_foodness_photo", fake_foodness)
    monkeypatch.setattr(foodai_module, "_openai_request", fake_openai_request)

    # Case 1: use Responses for gpt-5
    foodai_module.settings.FOODAI_VISION_MODEL = "gpt-5.1-mini"
    foodai_module.settings.FOODAI_USE_RESPONSES_FOR_5 = True
    calls.clear()
    res1 = await analyze_photo("fake_photo_id")
    assert isinstance(res1, dict) and not res1.get("error")
    # With Visual Facts enabled, the first call is Facts (responses), the second is main (responses)
    assert len(calls) >= 2 and calls[0] == "responses" and calls[1] == "responses"

    # Case 2: use Chat for gpt-5
    foodai_module.settings.FOODAI_VISION_MODEL = "gpt-5.1-mini"
    foodai_module.settings.FOODAI_USE_RESPONSES_FOR_5 = False
    calls.clear()
    res2 = await analyze_photo("fake_photo_id")
    assert isinstance(res2, dict) and not res2.get("error")
    # With Visual Facts enabled, the first call is Facts (responses), the second is main (chat)
    assert len(calls) >= 2 and calls[0] == "responses" and calls[1] == "chat"


@pytest.mark.asyncio
async def test_refine_meal_change_qty_short_ml():
    base = {
        "title": "Блюдо",
        "calories": 300,
        "protein_g": 15.0,
        "fat_g": 10.0,
        "carbs_g": 30.0,
        "weight_g": 300.0,
        "items": [
            {"name": "кефир", "weight_g": 100.0},
        ],
    }
    out = await refine_meal(base, "кефир +200 мл")
    assert not out.get("error")
    it = next((i for i in out.get("items", []) if i.get("name", "").lower().startswith("кефир")), None)
    assert it is not None
    assert 195.0 <= float(it.get("weight_g") or 0) <= 215.0
    assert it.get("is_liquid") is True
    app = it.get("appearance")
    assert app and app.get("unit") == "ml" and app.get("qty") == 200.0


@pytest.mark.asyncio
async def test_refine_meal_change_qty_pcs_unknown_fallback():
    base = {
        "title": "Блюдо",
        "calories": 300,
        "protein_g": 15.0,
        "fat_g": 10.0,
        "carbs_g": 30.0,
        "weight_g": 300.0,
        "items": [
            {"name": "печенье", "weight_g": 20.0},
        ],
    }
    out = await refine_meal(base, "печенье 3 шт")
    assert not out.get("error")
    it = next((i for i in out.get("items", []) if i.get("name", "").lower().startswith("печен")), None)
    assert it is not None
    assert 290.0 <= float(it.get("weight_g") or 0) <= 310.0  # 3 * 100 г фоллбэк
    app = it.get("appearance")
    assert app and app.get("unit") == "шт" and app.get("qty") == 3.0


@pytest.mark.asyncio
async def test_refine_meal_change_qty_ml_cap_upper():
    base = {
        "title": "Блюдо",
        "calories": 300,
        "protein_g": 15.0,
        "fat_g": 10.0,
        "carbs_g": 30.0,
        "weight_g": 300.0,
        "items": [
            {"name": "кефир", "weight_g": 100.0},
        ],
    }
    out = await refine_meal(base, "кефир +2000 мл")
    assert not out.get("error")
    it = next((i for i in out.get("items", []) if i.get("name", "").lower().startswith("кефир")), None)
    assert it is not None
    assert float(it.get("weight_g") or 0) == 1000.0  # кап до 1000 г
    assert it.get("is_liquid") is True
    app = it.get("appearance")
    assert app and app.get("unit") == "ml" and app.get("qty") == 2000.0


@pytest.mark.asyncio
async def test_refine_meal_change_qty_short_grams():
    base = {
        "title": "Блюдо",
        "calories": 300,
        "protein_g": 15.0,
        "fat_g": 10.0,
        "carbs_g": 30.0,
        "weight_g": 300.0,
        "items": [
            {"name": "рис", "weight_g": 200.0},
        ],
    }
    out = await refine_meal(base, "рис 150 г")
    assert not out.get("error")
    it = next((i for i in out.get("items", []) if i.get("name", "").lower().startswith("рис")), None)
    assert it is not None
    assert float(it.get("weight_g") or 0) == 150.0
    assert it.get("is_liquid") in (None, False)


@pytest.mark.asyncio
async def test_refine_meal_estimate_from_name_rice_200g():
    base = {
        "title": "Блюдо",
        "calories": 0,
        "protein_g": 0.0,
        "fat_g": 0.0,
        "carbs_g": 0.0,
        "weight_g": 0.0,
        "items": [
            {"name": "рис", "weight_g": 50.0},  # макросы отсутствуют → будут оценены
        ],
    }
    out = await refine_meal(base, "рис 200 г")
    assert not out.get("error")
    it = next((i for i in out.get("items", []) if i.get("name", "").lower().startswith("рис")), None)
    assert it is not None
    # Пер100г: кал 130, бел 2.7, жир 0.3, угл 28.0 → на 200 г: 260, 5.4, 0.6, 56.0
    assert int(it.get("calories") or 0) == 260
    assert abs(float(it.get("protein_g") or 0) - 5.4) <= 0.2
    assert abs(float(it.get("fat_g") or 0) - 0.6) <= 0.2
    assert abs(float(it.get("carbs_g") or 0) - 56.0) <= 0.5
