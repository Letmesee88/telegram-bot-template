import asyncio
import json
import types

import pytest

from bot.services.foodai import _normalize_openai_json, _openai_request


# -------------------------
# Tests for _normalize_openai_json
# -------------------------

def test_normalize_plain_json():
    raw = json.dumps({
        "title": "борщ",
        "calories": 250,
        "protein_g": 8.5,
        "fat_g": 12.3,
        "carbs_g": 22.7,
        "weight_g": 350,
        "confidence": 0.82,
        "items": [
            {"name": "борщ", "calories": 200, "protein_g": 7, "fat_g": 9, "carbs_g": 20, "weight_g": 300, "is_liquid": True}
        ],
        "references": {"sources": ["ФГБУН \"ФИЦ питания и биотехнологии\"", "USDA FoodData Central"]},
        "analysis_text": "Тестовый текст",
        "appearance": {"is_packaged": False, "plate_visible": True, "plate_diameter_cm": 24},
        "not_food": False,
    })
    out = _normalize_openai_json(raw)
    assert out is not None
    assert out["title"] == "борщ"
    assert out["calories"] == 250
    assert isinstance(out["appearance"], dict)


def test_normalize_code_fence_json():
    raw = """```json\n{\n  \"is_food\": true\n}\n```"""
    out = _normalize_openai_json(raw)
    # _normalize_openai_json expects the full food result schema, so for a minimal object
    # it should fail and return None. This asserts graceful failure on non-conforming JSON.
    assert out is None


# -------------------------
# Test for _openai_request extraction logic
# Case: empty output_text, JSON sits in output[].content[].text
# -------------------------

class DummyHTTPResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)


class DummyPostCM:
    def __init__(self, response: DummyHTTPResponse):
        self._resp = response

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, headers=None, data=None, timeout=None):
        body = {
            "output_text": "",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "{\"is_food\": true}"}
                    ],
                }
            ],
        }
        return DummyPostCM(DummyHTTPResponse(200, body))


@pytest.mark.asyncio
async def test_openai_request_uses_output_array(monkeypatch):
    # Patch ClientSession used in _openai_request
    monkeypatch.setattr("bot.services.foodai.ClientSession", DummyClientSession)

    payload = {
        "model": "gpt-5-mini",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "ping"}]}
        ],
        # minimal fields to match the code path; headers/URL are irrelevant due to patch
    }
    content = await _openai_request("responses", payload)
    assert content is not None
    assert json.loads(content) == {"is_food": True}


# -------------------------
# Additional full-case tests for _normalize_openai_json
# -------------------------

def test_normalize_full_foodai_result_multiple_items_and_refs():
    raw = json.dumps({
        "title": "гречка с курицей",
        "calories": 520,
        "protein_g": 35.0,
        "fat_g": 16.0,
        "carbs_g": 60.0,
        "weight_g": 450,
        "confidence": 0.88,
        "items": [
            {"name": "гречка", "calories": 300, "protein_g": 12, "fat_g": 4, "carbs_g": 56, "weight_g": 300, "is_liquid": False},
            {"name": "курица", "calories": 220, "protein_g": 23, "fat_g": 12, "carbs_g": 4, "weight_g": 150, "is_liquid": False},
        ],
        "references": {"sources": ["ФГБУН \"ФИЦ питания и биотехнологии\"", "USDA FoodData Central"]},
        "analysis_text": "На фото гречка и курица. Текст для проверки длины и безопасного парсинга.",
        "appearance": {"is_packaged": False, "plate_visible": True, "plate_diameter_cm": 24},
        "not_food": False,
    })
    out = _normalize_openai_json(raw)
    assert out is not None
    assert out["title"].startswith("гречка")
    assert out["calories"] == 520
    assert isinstance(out["items"], list) and len(out["items"]) == 2
    # plate_diameter_cm must be int
    assert isinstance(out["appearance"].get("plate_diameter_cm"), int)
    # references.sources must be exactly 2 and exact strings
    refs = out["references"].get("sources")
    assert isinstance(refs, list) and len(refs) == 2
    assert refs[0] == "ФГБУН \"ФИЦ питания и биотехнологии\""
    assert refs[1] == "USDA FoodData Central"


def test_normalize_plate_diameter_non_numeric_sets_none():
    raw = json.dumps({
        "title": "салат",
        "calories": 150,
        "protein_g": 5,
        "fat_g": 7,
        "carbs_g": 14,
        "weight_g": 200,
        "confidence": 0.7,
        "items": [],
        "references": {"sources": ["ФГБУН \"ФИЦ питания и биотехнологии\"", "USDA FoodData Central"]},
        "analysis_text": "",
        "appearance": {"is_packaged": False, "plate_visible": False, "plate_diameter_cm": "NaN"},
        "not_food": False,
    })
    out = _normalize_openai_json(raw)
    assert out is not None
    assert out["appearance"].get("plate_diameter_cm") is None
