from __future__ import annotations

import pytest

from bot.handlers.foodai import _build_preview_text
import bot.handlers.foodai as foodai_module


def test_preview_title_bold_html_and_spacing_text(monkeypatch: pytest.MonkeyPatch):
    # Stub i18n gettext to avoid requiring aiogram I18n context
    monkeypatch.setattr(foodai_module, "_", lambda s: s, raising=False)
    title = "Чашка чёрного кофе"
    out = _build_preview_text(
        cal=2,
        p=0.3,
        f=0.0,
        c=0.0,
        conf=0.7,
        weight=240.0,
        items=[{"name": "Кофе чёрный, заваренный", "calories": 2, "weight_g": 240, "is_liquid": True}],
        references={"sources": ["ФГБУН \"ФИЦ питания и биотехнологии\"", "USDA FoodData Central"]},
        title=title,
        source="text",
        itogo={"p_pct": 0.1, "f_pct": 0, "c_pct": 0, "cal_pct": 0.1},
        analysis_text="На фото Кофе чёрный, заваренный. Использованы справочные данные ФИЦ питания и USDA.",
    )

    # Header line should be present
    assert "📝 Анализ описания завершен!" in out
    # Ensure one blank line between header and title, and title bolded in HTML
    assert "\n\n<b>Чашка чёрного кофе</b>\n" in out


def test_preview_title_bold_html_and_spacing_photo(monkeypatch: pytest.MonkeyPatch):
    # Stub i18n gettext to avoid requiring aiogram I18n context
    monkeypatch.setattr(foodai_module, "_", lambda s: s, raising=False)
    title = "Салат с курицей"
    out = _build_preview_text(
        cal=250,
        p=20.0,
        f=10.0,
        c=18.0,
        conf=0.9,
        weight=300.0,
        items=[{"name": "Курица", "calories": 120, "weight_g": 150}],
        references={"sources": ["ФГБУН \"ФИЦ питания и биотехнологии\"", "USDA FoodData Central"]},
        title=title,
        source="photo",
        itogo=None,
        analysis_text="На фото салат с курицей. Использованы справочные данные ФИЦ питания и USDA.",
    )

    assert "👌🏼 Анализ фото готов !" in out
    assert "\n\n<b>Салат с курицей</b>\n" in out
