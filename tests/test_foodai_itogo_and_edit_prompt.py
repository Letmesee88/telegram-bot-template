from __future__ import annotations

import pytest

from bot.handlers.foodai import _build_preview_text, _build_edit_prompt_text
import bot.handlers.foodai as foodai_module


def test_itogo_operator_precedence_and_values(monkeypatch: pytest.MonkeyPatch):
    # Stub i18n gettext to avoid requiring aiogram I18n context
    monkeypatch.setattr(foodai_module, "_", lambda s: s, raising=False)

    out = _build_preview_text(
        cal=300,
        p=25.0,
        f=10.0,
        c=35.0,
        conf=0.9,
        weight=300.0,
        items=[{"name": "Курица", "calories": 120, "weight_g": 150}],
        references={"sources": ["ФГБУН \"ФИЦ питания и биотехнологии\"", "USDA FoodData Central"]},
        title="Салат с курицей",
        source="text",
        itogo={
            # pct of daily plan for this meal
            "cal_pct": 120,   # -> delta = +20
            "p_pct": 110.5,   # -> delta = +10.5
            "f_pct": 100,     # -> delta = 0
            "c_pct": 95,      # -> delta = -5 (excess in current _fmt semantics)
        },
        analysis_text="На фото салат с курицей. Использованы справочные данные ФИЦ питания и USDA.",
    )

    # Ensure 'Итого' section present
    assert "\n📊 Итого:\n" in out

    # Analysis paragraph must NOT mention confidence word
    assert "Уверенность" not in out

    # Calories: 120% -> (+20 ккал превышено)
    assert "⚠️ 🔥 Калории: 120% (+20 ккал превышено)" in out

    # Proteins: 110.5% -> (+10.5 г превышено)
    assert "⚠️ 🥩 Белки: 110.5% (+10.5 г превышено)" in out

    # Fats: 100% -> норма достигнута
    assert "🥑 Жиры: 100% (норма достигнута)" in out

    # Carbs: 95% -> (-5.0 г до нормы)
    assert "🍞 Углеводы: 95% (-5.0 г до нормы)" in out


def test_edit_prompt_proteins_spacing(monkeypatch: pytest.MonkeyPatch):
    # Stub i18n gettext
    monkeypatch.setattr(foodai_module, "_", lambda s: s, raising=False)

    out = _build_edit_prompt_text(
        cal=123,
        p=12.3,
        f=4.5,
        c=67.8,
        weight=250.0,
        items=[{"name": "Курица", "weight_g": 150}],
        title="Салат с курицей",
    )

    # No double space before 'г'
    assert "🥩 Белки: 12.3 г" in out
    assert "Белки: 12.3  г" not in out
