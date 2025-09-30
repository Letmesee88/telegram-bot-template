from __future__ import annotations

from bot.core.config import settings


def test_env_settings_for_gpt5_stable_mode():
    # Provider/model consistency
    assert (settings.FOODAI_PROVIDER or "").lower() == "openai"
    assert settings.FOODAI_DEFAULT_MODEL == "gpt-5-mini-2025-08-07"
    assert settings.FOODAI_EDIT_MODEL == "gpt-5-mini-2025-08-07"
    assert settings.FOODAI_VISION_MODEL == "gpt-5-mini-2025-08-07"

    # API modes: Responses enabled (including GPT-5 photo via Responses)
    assert (settings.FOODAI_API or "").lower() == "responses"
    assert settings.FOODAI_USE_RESPONSES_FOR_5 is True

    # Vision detail and escalation controls (current production preset)
    assert (settings.FOODAI_IMAGE_DETAIL or "").lower() == "low"
    assert (settings.FOODAI_VISION_DETAIL_ORDER or "").lower() == "low"
    assert settings.FOODAI_IMAGE_DETAIL_HIGH_RETRY is True

    assert settings.FOODAI_VISION_ESCALATION_ENABLED is True
    assert int(settings.FOODAI_VISION_MAX_STEPS or 0) == 2
    assert (settings.FOODAI_VISION_ESCALATION_CHAIN or "") != ""

    # No fallback to 4o-mini when we insist on GPT-5
    assert settings.FOODAI_ALLOW_FALLBACK_TO_4O_MINI is False
