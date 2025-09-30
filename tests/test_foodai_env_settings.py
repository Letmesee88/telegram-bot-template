from __future__ import annotations

from bot.core.config import settings


def test_env_settings_for_gpt5_stable_mode():
    # Provider/model consistency
    assert (settings.FOODAI_PROVIDER or "").lower() == "openai"
    assert settings.FOODAI_DEFAULT_MODEL == "gpt-5-mini-2025-08-07"
    assert settings.FOODAI_EDIT_MODEL == "gpt-5-mini-2025-08-07"
    assert settings.FOODAI_VISION_MODEL == "gpt-5-mini-2025-08-07"

    # API modes: responses allowed for text, but GPT-5 photo must go via Chat (so detail applies)
    assert (settings.FOODAI_API or "").lower() == "responses"
    assert settings.FOODAI_USE_RESPONSES_FOR_5 is False

    # Vision detail and escalation controls
    assert (settings.FOODAI_IMAGE_DETAIL or "").lower() == "high"
    assert (settings.FOODAI_VISION_DETAIL_ORDER or "").lower() == "high"
    assert settings.FOODAI_IMAGE_DETAIL_HIGH_RETRY is False

    assert settings.FOODAI_VISION_ESCALATION_ENABLED is False
    assert int(settings.FOODAI_VISION_MAX_STEPS or 0) == 1
    assert (settings.FOODAI_VISION_ESCALATION_CHAIN or "") == ""

    # No fallback to 4o-mini when we insist on GPT-5
    assert settings.FOODAI_ALLOW_FALLBACK_TO_4O_MINI is False
