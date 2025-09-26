from __future__ import annotations

import pytest


def _call_preview(module, *, conf: float, show_labels: bool, show_hint: bool, high_thr: float | None = None, low_thr: float | None = None) -> str:
    # Ensure i18n passthrough
    module._ = lambda s: s  # type: ignore[attr-defined]
    # Configure flags and thresholds
    module.settings.FOODAI_SHOW_CONF_LABELS = show_labels
    module.settings.FOODAI_SHOW_LOW_CONF_HINT = show_hint
    if high_thr is not None:
        module.settings.FOODAI_CONF_HIGH = float(high_thr)
    if low_thr is not None:
        module.settings.FOODAI_CONF_LOW = float(low_thr)
    module.settings.FOODAI_ESCALATE_CONF = 0.70

    # Minimal, deterministic input
    text = module._build_preview_text(
        cal=720,
        p=36.5,
        f=50.0,
        c=30.0,
        conf=conf,
        weight=420.0,
        items=[{"name": "Омлет", "weight_g": 140, "calories": 220}],
        references={"source": "stub"},
        title="Завтрак",
        source="photo",
        analysis_text="Тестовый анализ",
    )
    assert isinstance(text, str)
    return text


@pytest.mark.parametrize(
    "conf, show_labels, expect_label",
    [
        (0.62, False, False),  # labels disabled
        (0.55, False, False),  # labels disabled
        (0.62, True, True),    # labels enabled, falls into 'средняя' by defaults (0.6/0.8)
    ],
)
def test_preview_confidence_labels_toggle(monkeypatch: pytest.MonkeyPatch, conf: float, show_labels: bool, expect_label: bool) -> None:
    from bot.handlers import foodai as fh

    txt = _call_preview(fh, conf=conf, show_labels=show_labels, show_hint=False)

    has_label = ("Уверенность:" in txt)
    assert has_label == expect_label


def test_preview_low_conf_hint_respects_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import foodai as fh

    # Case 1: hint on, conf below threshold -> hint shown
    txt1 = _call_preview(fh, conf=0.62, show_labels=False, show_hint=True)
    assert "Внимание: низкая уверенность" in txt1

    # Case 2: hint off -> no hint even below threshold
    txt2 = _call_preview(fh, conf=0.62, show_labels=False, show_hint=False)
    assert "Внимание: низкая уверенность" not in txt2


def test_preview_high_conf_no_label_when_above_high_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import foodai as fh

    # Raise high threshold behavior check: with HIGH=0.75 and conf=0.77 -> no label
    txt = _call_preview(fh, conf=0.77, show_labels=True, show_hint=False, high_thr=0.75)
    assert "Уверенность:" not in txt
