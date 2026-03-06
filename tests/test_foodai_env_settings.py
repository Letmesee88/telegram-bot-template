from __future__ import annotations
import os

from bot.core import config as cfg


def test_env_settings_foodai_bd_mode() -> None:
    # Clear potential ENV overrides set by other tests
    for key in [
        "FOODAI_PROVIDER",
        "FOODAI_API",
        "FOODAI_USE_RESPONSES_FOR_5",
        "FOODAI_VISION_MODEL",
        "FOODAI_IMAGE_DETAIL",
        "FOODAI_IMAGE_DETAIL_HIGH_RETRY",
        "FOODAI_VISION_ESCALATION_ENABLED",
        "FOODAI_VISION_MAX_STEPS",
        "FOODAI_VISION_DETAIL_ORDER",
        "FOODAI_ALLOW_FALLBACK_TO_4O_MINI",
        "FOODAI_TEXT_FALLBACK_TO_CHAT",
        "FOODAI_FACTS_ENABLED",
        "FOODAI_FACTS_MAX_TOKENS",
    ]:
        os.environ.pop(key, None)
    # Force deterministic B + D config for this test via env overrides
    os.environ.update({
        "FOODAI_PROVIDER": "openai",
        "FOODAI_API": "responses",
        "FOODAI_USE_RESPONSES_FOR_5": "true",
        "FOODAI_IMAGE_DETAIL": "high",
        "FOODAI_VISION_ESCALATION_ENABLED": "false",
        "FOODAI_VISION_MAX_STEPS": "1",
        "FOODAI_VISION_DETAIL_ORDER": "high",
        "FOODAI_ALLOW_FALLBACK_TO_4O_MINI": "false",
        "FOODAI_TEXT_FALLBACK_TO_CHAT": "false",
        "FOODAI_FACTS_ENABLED": "true",
        "FOODAI_FACTS_MAX_TOKENS": "400",
    })
    # Recreate settings to read fresh from env/.env and avoid leaks from previous tests
    cfg.settings = cfg.Settings()
    settings = cfg.settings
    # Provider/model consistency
    assert (settings.FOODAI_PROVIDER or "").lower() == "openai"
    # Photo analysis uses explicit vision model from .env; default model may differ
    assert isinstance(settings.FOODAI_VISION_MODEL, str)
    assert settings.FOODAI_VISION_MODEL.startswith("gpt-5")

    # API modes: Responses enabled (including GPT-5 photo via Responses)
    assert (settings.FOODAI_API or "").lower() == "responses"
    assert settings.FOODAI_USE_RESPONSES_FOR_5 is True

    # Vision detail and escalation controls (B + D config)
    assert (settings.FOODAI_IMAGE_DETAIL or "").lower() in {"high", "auto"}
    assert settings.FOODAI_VISION_ESCALATION_ENABLED is False
    assert int(settings.FOODAI_VISION_MAX_STEPS or 0) == 1
    # No chat fallback
    assert settings.FOODAI_TEXT_FALLBACK_TO_CHAT is False
    # Visual Facts enabled
    assert settings.FOODAI_FACTS_ENABLED is True
    assert int(settings.FOODAI_FACTS_MAX_TOKENS or 0) > 0
