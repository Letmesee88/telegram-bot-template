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
        self.url = None


class DummyMessage:
    def __init__(self, user_id: int = 123) -> None:
        self.captured: dict[str, object] = {}
        self.from_user = DummyUser(user_id)
        class _DummyChat:
            def __init__(self) -> None:
                self.id = 1
                self.type = "private"

        self.chat = _DummyChat()

    async def answer(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:  # type: ignore[override]
        self.captured["text"] = text
        self.captured["reply_markup"] = reply_markup


class DummyState:
    def __init__(self, state: str | None = None) -> None:
        self._state = state

    async def get_state(self) -> str | None:  # aiogram FSMContext compat subset
        return self._state


@pytest.mark.asyncio
async def test_start_handler_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics
    from bot.services import analytics as analytics_module

    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    from bot.handlers import start as start_module

    # Avoid aiogram I18n context by replacing _ with identity
    monkeypatch.setattr(start_module, "_", lambda s: s)

    # Not completed in DB
    class _FakeSession:
        async def scalar(self, _query):
            return None

    class _FakeSM:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(start_module, "sessionmaker", lambda: _FakeSM())

    msg = DummyMessage(user_id=1001)
    state = DummyState(state="onboarding:goal")  # simulate FSM in progress

    await start_module.start_handler(msg, state)  # type: ignore[arg-type]

    text = msg.captured.get("text")
    assert isinstance(text, str)
    assert "Привет!" in text
    assert "Приступим?" in text
    assert "Хочешь его сбросить" not in text

    kb = msg.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    datas = {btn.callback_data for btn in buttons}
    texts = {btn.text for btn in buttons}

    assert datas == {"onboarding_start"}
    assert texts == {"Начнем"}


@pytest.mark.asyncio
async def test_start_handler_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics
    from bot.services import analytics as analytics_module

    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    from bot.handlers import start as start_module
    
    # Avoid aiogram I18n context by replacing _ with identity
    monkeypatch.setattr(start_module, "_", lambda s: s)

    # Not completed in DB
    class _FakeSession:
        async def scalar(self, _query):
            return None

    class _FakeSM:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(start_module, "sessionmaker", lambda: _FakeSM())

    msg = DummyMessage(user_id=1002)
    state = DummyState(state=None)  # fresh, no FSM

    await start_module.start_handler(msg, state)  # type: ignore[arg-type]

    text = msg.captured.get("text")
    assert isinstance(text, str)
    assert "Привет!" in text
    assert "Приступим?" in text
    assert "Хочешь его сбросить" not in text

    kb = msg.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    datas = {btn.callback_data for btn in buttons}
    texts = {btn.text for btn in buttons}

    assert datas == {"onboarding_start"}
    assert texts == {"Начнем"}


@pytest.mark.asyncio
async def test_start_no_callback_sends_explicit_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import start as start_module

    # Avoid aiogram I18n context by replacing _ with identity
    monkeypatch.setattr(start_module, "_", lambda s: s)

    answered = {"ok": False}

    class _DummyCallMessage(DummyMessage):
        pass

    class _DummyCallback:
        def __init__(self):
            self.message = _DummyCallMessage()

        async def answer(self):
            answered["ok"] = True

    call = _DummyCallback()

    await start_module.start_no(call)  # type: ignore[arg-type]

    assert answered["ok"] is True
    text = call.message.captured.get("text")
    assert isinstance(text, str)
    assert "оставляем текущий план" in text.lower()


class _DummyLogger:
    def __init__(self) -> None:
        self.events = []

    async def log_event(self, event):  # type: ignore[no-untyped-def]
        self.events.append(event)


@pytest.mark.asyncio
async def test_start_analytics_completed_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.services import analytics as analytics_module
    from bot.handlers import start as start_module

    # I18n noop
    monkeypatch.setattr(start_module, "_", lambda s: s)

    # Force DB completed=True
    class _FakeSession:
        async def scalar(self, _query):
            return 1

    class _FakeSM:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(start_module, "sessionmaker", lambda: _FakeSM())

    # Capture analytics
    dummy = _DummyLogger()
    # Disable decorator logging and bypass wrapper; capture only internal Start Session
    monkeypatch.setattr(analytics_module.analytics, "logger", None)
    monkeypatch.setattr(start_module.analytics, "logger", dummy)
    monkeypatch.setattr(start_module, "start_handler", start_module.start_handler.__wrapped__)

    msg = DummyMessage(user_id=2001)
    state = DummyState(state=None)

    await start_module.start_handler(msg, state)  # type: ignore[arg-type]

    start_events = [e for e in dummy.events if getattr(e, "event_type", None) == "Start Session"]
    assert len(start_events) == 1
    e = start_events[0]
    assert e.plan is not None and e.plan.branch == "Completed"
    assert e.plan.source == "start" and e.plan.version == "v1"
    assert e.event_properties and e.event_properties.command == "/start"


@pytest.mark.asyncio
async def test_start_analytics_in_progress_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.services import analytics as analytics_module
    from bot.handlers import start as start_module

    # I18n noop
    monkeypatch.setattr(start_module, "_", lambda s: s)

    # Not completed in DB
    class _FakeSession:
        async def scalar(self, _query):
            return None

    class _FakeSM:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(start_module, "sessionmaker", lambda: _FakeSM())

    dummy = _DummyLogger()
    monkeypatch.setattr(analytics_module.analytics, "logger", None)
    monkeypatch.setattr(start_module.analytics, "logger", dummy)
    monkeypatch.setattr(start_module, "start_handler", start_module.start_handler.__wrapped__)

    msg = DummyMessage(user_id=2002)
    state = DummyState(state="onboarding:any")

    await start_module.start_handler(msg, state)  # type: ignore[arg-type]

    start_events = [e for e in dummy.events if getattr(e, "event_type", None) == "Start Session"]
    assert len(start_events) == 1
    e = start_events[0]
    assert e.plan is not None and e.plan.branch == "InProgress"
    assert e.plan.source == "start" and e.plan.version == "v1"


@pytest.mark.asyncio
async def test_start_analytics_fresh_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.services import analytics as analytics_module
    from bot.handlers import start as start_module

    # I18n noop
    monkeypatch.setattr(start_module, "_", lambda s: s)

    # Not completed in DB
    class _FakeSession:
        async def scalar(self, _query):
            return None

    class _FakeSM:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(start_module, "sessionmaker", lambda: _FakeSM())

    dummy = _DummyLogger()
    monkeypatch.setattr(analytics_module.analytics, "logger", None)
    monkeypatch.setattr(start_module.analytics, "logger", dummy)
    monkeypatch.setattr(start_module, "start_handler", start_module.start_handler.__wrapped__)

    msg = DummyMessage(user_id=2003)
    state = DummyState(state=None)

    await start_module.start_handler(msg, state)  # type: ignore[arg-type]

    start_events = [e for e in dummy.events if getattr(e, "event_type", None) == "Start Session"]
    assert len(start_events) == 1
    e = start_events[0]
    assert e.plan is not None and e.plan.branch == "Fresh"
    assert e.plan.source == "start" and e.plan.version == "v1"
