from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_preview_hides_sources_analysis_itogo_on_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import foodai as fh

    # i18n passthrough and flags
    fh._ = lambda s: s  # type: ignore[attr-defined]
    fh.settings.FOODAI_SHOW_CONF_LABELS = True
    fh.settings.FOODAI_SHOW_LOW_CONF_HINT = True
    fh.settings.FOODAI_CONF_HIGH = 0.80
    fh.settings.FOODAI_CONF_LOW = 0.60
    fh.settings.FOODAI_ESCALATE_CONF = 0.70

    txt = fh._build_preview_text(
        cal=500,
        p=20.0,
        f=15.0,
        c=55.0,
        conf=0.55,
        weight=300.0,
        items=[{"name": "Сэндвич", "weight_g": 200, "calories": 450}],
        references={"source": "stub"},
        title="Обед",
        source="edit",  # IMPORTANT: edit mode
        analysis_text="Анализ, который должен быть скрыт",
        itogo={"cal_pct": 25.0, "p_pct": 20.0, "f_pct": 30.0, "c_pct": 22.0},
    )

    assert isinstance(txt, str)
    # Always present
    assert "Калории:" in txt and "⚖️ Вес:" in txt

    # Hidden blocks for edit
    assert "------------------------------" not in txt
    assert "📋 Источники данных:" not in txt
    assert "🔍 Анализ:" not in txt
    assert "Уверенность:" not in txt and "Внимание: низкая уверенность" not in txt
    assert "📊 Итого:" not in txt
