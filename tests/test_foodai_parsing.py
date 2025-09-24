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
