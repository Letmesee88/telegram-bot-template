from __future__ import annotations

import asyncio
import pytest
from bot.services import foodai as foodai_mod


def test_foodai_photo_flow_basic(monkeypatch):
    """Self-contained photo flow test without DB or docker.
    Wraps async logic in asyncio.run to avoid pytest-asyncio dependency.
    """

    async def _run():
        # 1) Force provider path
        monkeypatch.setattr(foodai_mod, "_use_openai", lambda: True)

        async def fake_tg_file_url(fid: str) -> str:
            return "https://api.telegram.org/file/botTOKEN/photos/file_9.jpg"

        monkeypatch.setattr(foodai_mod, "_tg_file_url", fake_tg_file_url)

        async def fake_openai_request(kind: str, payload: dict):
            return (
                """
                {
                  "calories": 550,
                  "protein_g": 30.0,
                  "fat_g": 35.0,
                  "carbs_g": 40.0,
                  "weight_g": 250.0,
                  "confidence": 0.85,
                  "items": [
                    {"name": "burger", "calories": 300, "protein_g": 20.0, "fat_g": 15.0, "carbs_g": 25.0}
                  ],
                  "references": { "sources": ["USDA FoodData Central"] }
                }
                """
            )

        monkeypatch.setattr(foodai_mod, "_openai_request", fake_openai_request)

        # Ensure pre-check passes
        async def fake_foodness_photo(url: str):
            return True

        monkeypatch.setattr(foodai_mod, "_foodness_photo", fake_foodness_photo)

        # 2) Run analyze and validate structure
        res = await foodai_mod.analyze_photo("FAKE_FILE_ID")

        assert isinstance(res, dict)
        keys = {"calories", "protein_g", "fat_g", "carbs_g", "weight_g", "confidence", "items", "references"}
        assert keys.issubset(res.keys())

        assert int(res["calories"]) > 0
        assert 0.0 <= float(res["confidence"]) <= 1.0
        assert isinstance(res["items"], list)

    asyncio.run(_run())
