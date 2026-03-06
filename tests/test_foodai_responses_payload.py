from __future__ import annotations
import json

import pytest


@pytest.mark.asyncio
async def test_gpt5_responses_payload_excludes_temperature_top_p(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.services import foodai as svc

    # Ensure we hit the Responses branch for GPT-5
    svc.settings.FOODAI_PROVIDER = "openai"
    svc.settings.FOODAI_USE_RESPONSES_FOR_5 = True
    svc.settings.FOODAI_VISION_MODEL = "gpt-5-mini-2025-08-07"
    svc.settings.FOODAI_IMAGE_DETAIL = "low"
    svc.settings.FOODAI_IMAGE_DETAIL_HIGH_RETRY = False  # avoid extra retry paths

    # Stub Telegram URL resolution and precheck with async fakes
    async def _fake_tg_file_url(_file_id: str) -> str:
        return "https://example.com/file.jpg"

    async def _fake_foodness_photo(_url: str) -> bool:
        return True

    monkeypatch.setattr(svc, "_tg_file_url", _fake_tg_file_url)
    monkeypatch.setattr(svc, "_foodness_photo", _fake_foodness_photo)

    class _Stop(Exception):
        pass

    async def _fake_openai_request(kind: str, payload: dict[str, object]) -> str | None:
        # Precheck uses name=foodness
        if kind == "responses":
            try:
                fmt = (((payload or {}).get("text") or {}).get("format") or {})  # type: ignore[union-attr]
                name = fmt.get("name") if isinstance(fmt, dict) else None
            except Exception:
                name = None
            # Validate analysis payload (matches code: schema name is 'foodai_result')
            if name == "foodai_result":
                # Forbidden params must be absent for GPT-5 Responses
                assert "temperature" not in payload
                assert "top_p" not in payload
                # Required GPT-5-specific controls should be present
                assert "reasoning" in payload
                assert "text" in payload
                assert "max_output_tokens" in payload
                # Stop the flow after validation
                raise _Stop
        # Return OK result for other calls (e.g., precheck)
        if kind == "responses":
            return json.dumps({"is_food": True})
        return None

    # Patch the network layer
    monkeypatch.setattr(svc, "_openai_request", _fake_openai_request)

    with pytest.raises(_Stop):
        await svc.analyze_photo("dummy_file_id")
