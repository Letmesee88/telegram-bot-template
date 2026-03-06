from __future__ import annotations
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup


class DummyUser:
    def __init__(self, user_id: int = 123) -> None:
        self.id = user_id
        self.language_code = "ru"


class DummyCBMessage:
    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    async def edit_caption(self, caption: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:  # type: ignore[override]
        self.captured["text"] = caption
        self.captured["reply_markup"] = reply_markup

    async def edit_text(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:  # type: ignore[override]
        self.captured["text"] = text
        self.captured["reply_markup"] = reply_markup


class DummyCallback:
    def __init__(self, data: str, user_id: int = 123) -> None:
        self.data = data
        self.from_user = DummyUser(user_id)
        self.message = DummyCBMessage()

    async def answer(self, *args, **kwargs) -> None:
        return None


class _FakeMealItem:
    def __init__(self, name: str, weight_g: float | None, calories: float | None) -> None:
        self.name = name
        self.weight_g = weight_g
        self.calories = calories


class _FakeMeal:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.title = "Сэндвич"
        self.calories = 450
        self.protein_g = 25.0
        self.fat_g = 18.0
        self.carbs_g = 35.0
        self.weight_g = 300.0
        self.items = [
            _FakeMealItem("хлеб", 120.0, 250.0),
            _FakeMealItem("ветчина", 60.0, 120.0),
        ]


class _FakeSession:
    def __init__(self, meal) -> None:
        self._meal = meal

    async def get(self, _model, _id):
        return self._meal


class _FakeSM:
    def __init__(self, meal) -> None:
        self._meal = meal

    async def __aenter__(self):
        return _FakeSession(self._meal)

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_cb_edit_detail_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    # i18n passthrough and disable analytics
    monkeypatch.setattr(menu_module, "_", lambda s: s)
    from bot.services import analytics as analytics_module
    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    # Fake session with meal owned by the user
    uid = 999
    meal = _FakeMeal(user_id=uid)
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meal))

    cb = DummyCallback(data="de:d:1:1", user_id=uid)
    await menu_module.cb_edit_detail(cb)  # type: ignore[arg-type]

    text = cb.message.captured.get("text")
    assert isinstance(text, str)
    lines = text.splitlines()

    # Blank line after title
    assert lines[0] == meal.title
    assert lines[1] == ""
    assert lines[2].startswith("🍜 Состав:")

    # Blank line between items and KBGU row
    last_item_idx = max(i for i, ln in enumerate(lines) if ln.startswith("• "))
    kbg_row_idx = next(i for i, ln in enumerate(lines) if ln.startswith("🔥 "))
    assert kbg_row_idx == last_item_idx + 2  # one empty line
    assert lines[last_item_idx + 1] == ""

    # Blank line before weight row
    if any(ln.startswith("⚖️ Вес:") for ln in lines):
        w_idx = next(i for i, ln in enumerate(lines) if ln.startswith("⚖️ Вес:"))
        assert lines[w_idx - 1] == ""


@pytest.mark.asyncio
async def test_cb_edit_detail_no_weight_no_extra_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    from bot.services import analytics as analytics_module
    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    class _MealNoWeight(_FakeMeal):
        def __init__(self, user_id: int) -> None:
            super().__init__(user_id)
            self.weight_g = 0.0

    uid = 1001
    meal = _MealNoWeight(user_id=uid)
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meal))

    cb = DummyCallback(data="de:d:1:1", user_id=uid)
    await menu_module.cb_edit_detail(cb)  # type: ignore[arg-type]

    text = cb.message.captured.get("text")
    assert isinstance(text, str)
    lines = text.splitlines()

    # There should be exactly one empty line between items and KBGU row
    last_item_idx = max(i for i, ln in enumerate(lines) if ln.startswith("• "))
    kbg_row_idx = next(i for i, ln in enumerate(lines) if ln.startswith("🔥 "))
    assert kbg_row_idx == last_item_idx + 2
    # And no trailing empty line after KBGU if weight is not shown
    assert not any(i > kbg_row_idx and lines[i] == "" for i in range(kbg_row_idx + 1, len(lines)))
