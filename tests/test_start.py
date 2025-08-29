from __future__ import annotations

import pytest
from aiogram.types import InlineKeyboardMarkup


class DummyUser:
    def __init__(self, user_id: int = 123):
        self.id = user_id
        self.first_name = "Test"
        self.last_name = None
        self.username = None
        self.language_code = "ru"


class DummyMessage:
    def __init__(self, user_id: int = 123) -> None:
        self.captured: dict[str, object] = {}
        self.from_user = DummyUser(user_id)

    async def answer(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:  # type: ignore[override]
        self.captured["text"] = text
        self.captured["reply_markup"] = reply_markup


class DummyState:
    def __init__(self, state: str | None = None) -> None:
        self._state = state

    async def get_state(self) -> str | None:  # aiogram FSMContext compat subset
        return self._state


@pytest.mark.asyncio
async def test_start_handler_sends_welcome_and_keyboard_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics network calls and skip wrapper logic
    from bot.services import analytics as analytics_module

    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    # Import after monkeypatch to ensure wrapper uses disabled analytics
    from bot.handlers import start as start_module

    # Avoid aiogram I18n context by replacing _ with identity
    monkeypatch.setattr(start_module, "_", lambda s: s)

    # Monkeypatch DB sessionmaker to simulate completed=True
    class _FakeSession:
        async def scalar(self, _query):
            return 1  # any truthy value means completed

    class _FakeSM:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(start_module, "sessionmaker", lambda: _FakeSM())

    start_handler = start_module.start_handler

    msg = DummyMessage(user_id=777)
    state = DummyState(state=None)

    await start_handler(msg, state)  # type: ignore[arg-type]

    # Validate text
    text = msg.captured.get("text")
    assert isinstance(text, str)
    assert "Привет!" in text
    assert "Хочешь его сбросить" in text

    # Validate keyboard
    kb = msg.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)

    # Flatten buttons
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    datas = {btn.callback_data for btn in buttons}
    texts = {btn.text for btn in buttons}

    assert "onboarding_start" in datas
    assert "start:no" in datas
    assert "Да" in texts
    assert "Нет" in texts
