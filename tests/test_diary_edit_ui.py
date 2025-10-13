from __future__ import annotations

import pytest
from datetime import datetime, timezone
from aiogram.types import InlineKeyboardMarkup


class DummyUser:
    def __init__(self, user_id: int = 123):
        self.id = user_id
        self.first_name = "Test"
        self.last_name = None
        self.username = None
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

    async def answer(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:  # fallback
        self.captured["text"] = text
        self.captured["reply_markup"] = reply_markup


class DummyCallback:
    def __init__(self, data: str, user_id: int = 123) -> None:
        self.data = data
        self.from_user = DummyUser(user_id)
        self.message = DummyCBMessage()

    async def answer(self, *args, **kwargs):
        return None


class _DT(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2025, 1, 2, 12, 0)
        return base.replace(tzinfo=tz)


class _FakeResult:
    def __init__(self, meals: list[object]):
        self._meals = meals

    class _Scalars:
        def __init__(self, meals: list[object]):
            self._meals = meals

        def all(self):
            return list(self._meals)

    def scalars(self):
        return _FakeResult._Scalars(self._meals)


class _FakeSession:
    def __init__(self, meals: list[object]):
        self._meals = meals

    async def execute(self, _query):
        return _FakeResult(self._meals)


class _FakeSM:
    def __init__(self, meals: list[object]):
        self._meals = meals

    async def __aenter__(self):
        return _FakeSession(self._meals)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMeal:
    def __init__(self, id: int, title: str, dt: datetime, cal=0):
        self.id = id
        self.title = title
        self.consumed_at = dt
        self.calories = cal


@pytest.mark.asyncio
async def test_cb_edit_list_numbered_text_and_single_column_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)

    async def _fake_tz(_session, _uid):
        return timezone.utc

    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    meals = [
        FakeMeal(1, "Сэндвич", datetime(2025, 1, 2, 8, 0, tzinfo=timezone.utc), cal=450),
        FakeMeal(2, "Омлет", datetime(2025, 1, 2, 9, 30, tzinfo=timezone.utc), cal=320),
        FakeMeal(3, "Салат", datetime(2025, 1, 2, 13, 0, tzinfo=timezone.utc), cal=200),
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals))

    cb = DummyCallback(data="de:l:1", user_id=777)
    await menu_module.cb_edit_list(cb)  # type: ignore[arg-type]

    text = cb.message.captured.get("text")
    assert isinstance(text, str)
    assert "Выберите блюдо" in text
    assert "1 " in text and "2 " in text and "3 " in text
    assert "1. " not in text  # no dot after index
    assert "→" in text and "ккал" in text

    kb = cb.message.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    rows = kb.inline_keyboard
    # n item rows, then optional nav row, then back row
    n = len(meals)
    assert len(rows[0]) == 1 and len(rows[1]) == 1 and len(rows[2]) == 1
    first_btn_texts = [rows[i][0].text for i in range(n)]
    assert first_btn_texts[0].startswith("1 ") and first_btn_texts[1].startswith("2 ") and first_btn_texts[2].startswith("3 ")
    first_btn_datas = [rows[i][0].callback_data for i in range(n)]
    assert all(d.startswith("de:d:") and d.endswith(":1") for d in first_btn_datas)
    assert rows[-1][0].text == "◀️ Вернуться назад"


@pytest.mark.asyncio
async def test_cb_edit_list_navigation_first_middle_last(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)

    async def _fake_tz(_session, _uid):
        return timezone.utc

    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    # 25 meals -> 3 pages (PAGE_SIZE=10)
    meals = [
        FakeMeal(i + 1, f"Meal {i+1}", datetime(2025, 1, 2, (i % 24), 0, tzinfo=timezone.utc), cal=100 + i)
        for i in range(25)
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals))

    # Page 1
    cb1 = DummyCallback(data="de:l:1", user_id=777)
    await menu_module.cb_edit_list(cb1)  # type: ignore[arg-type]
    kb1 = cb1.message.captured.get("reply_markup")
    assert isinstance(kb1, InlineKeyboardMarkup)
    texts1 = [btn.text for row in kb1.inline_keyboard for btn in row]
    assert "Вперёд ▶️" in texts1 and "◀️ Назад" not in texts1
    # First button should start with 1 
    assert kb1.inline_keyboard[0][0].text.startswith("1 ")

    # Page 2
    cb2 = DummyCallback(data="de:l:2", user_id=777)
    await menu_module.cb_edit_list(cb2)  # type: ignore[arg-type]
    kb2 = cb2.message.captured.get("reply_markup")
    assert isinstance(kb2, InlineKeyboardMarkup)
    texts2 = [btn.text for row in kb2.inline_keyboard for btn in row]
    assert "◀️ Назад" in texts2 and "Вперёд ▶️" in texts2
    # First item on page 2 should start with 11 
    assert kb2.inline_keyboard[0][0].text.startswith("11 ")

    # Page 3 (last)
    cb3 = DummyCallback(data="de:l:3", user_id=777)
    await menu_module.cb_edit_list(cb3)  # type: ignore[arg-type]
    kb3 = cb3.message.captured.get("reply_markup")
    assert isinstance(kb3, InlineKeyboardMarkup)
    texts3 = [btn.text for row in kb3.inline_keyboard for btn in row]
    assert "◀️ Назад" in texts3 and "Вперёд ▶️" not in texts3
