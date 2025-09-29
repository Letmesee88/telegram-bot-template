import pytest

from bot.handlers import onboarding as onboarding_mod


class DummyState:
    def __init__(self):
        self._data = {}

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def get_data(self):
        return dict(self._data)


class DummyChat:
    def __init__(self, chat_id=1, chat_type="private"):
        self.id = chat_id
        self.type = chat_type


class DummyMessage:
    def __init__(self, chat_id=1):
        self.chat = DummyChat(chat_id)

    async def answer(self, *args, **kwargs):
        return None


class DummyFromUser:
    def __init__(self, user_id=1, language_code="ru"):
        self.id = user_id
        self.language_code = language_code


class DummyCall:
    def __init__(self, data: str, user_id=1):
        self.data = data
        self.from_user = DummyFromUser(user_id)
        self.message = DummyMessage(chat_id=user_id)

    async def answer(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_speed_selected_fires_analytics(monkeypatch):
    state = DummyState()

    fired = {"called": False}

    def fake_fire_event(evt):
        fired["called"] = True

    # Monkeypatch analytics.fire_event in module scope
    monkeypatch.setattr(onboarding_mod.analytics, "logger", True, raising=False)
    monkeypatch.setattr(onboarding_mod.analytics, "fire_event", fake_fire_event, raising=False)

    # Stub out finalize to avoid heavy flow
    async def fake_finalize(message, state, user_id):
        return None

    monkeypatch.setattr(onboarding_mod, "_finalize_and_show", fake_finalize)

    call = DummyCall(data="speed:FAST", user_id=99)

    await onboarding_mod.cb_speed(call, state)

    assert fired["called"] is True
    data = await state.get_data()
    assert data.get("speed") == "FAST"
