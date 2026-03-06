# tests/test_foodai_text_flow.py
from __future__ import annotations
import asyncio

from bot.services import foodai as foodai_mod


def test_analyze_text_explicit_not_food(monkeypatch) -> None:
    """If model returns not_food=true for text, we propagate that (handler will short-circuit)."""

    async def _run() -> None:
        monkeypatch.setattr(foodai_mod, "_use_openai", lambda: True)

        async def fake_openai_request(kind: str, payload: dict) -> str:
            return (
                """
                {"title":"","calories":0,"protein_g":0.0,"fat_g":0.0,"carbs_g":0.0,"weight_g":0.0,
                 "confidence":0.0,
                 "items":[],
                 "references":{"sources":["ФГБУН \"ФИЦ питания и биотехнологии\"","USDA FoodData Central"]},
                 "analysis_text":null,
                 "appearance":{},
                 "not_food":true}
                """
            )

        monkeypatch.setattr(foodai_mod, "_openai_request", fake_openai_request)

        res = await foodai_mod.analyze_text("телефон")
        assert isinstance(res, dict)
        assert res.get("not_food") is True
        assert int(res.get("calories") or 0) == 0

    asyncio.run(_run())
