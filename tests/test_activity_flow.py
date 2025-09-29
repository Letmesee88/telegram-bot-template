import pytest

from bot.schemas.onboarding import OnboardingData, ActivityLevel, Goal, Gender
from bot.services.plan import calculate_daily_plan
from bot.handlers import onboarding as onboarding_mod


class DummyState:
    def __init__(self):
        self._data = {}
        self._state = None

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def set_state(self, state):
        # aiogram stores internal string value; we keep it as given
        self._state = state

    async def get_data(self):
        return dict(self._data)

    async def get_state(self):
        return self._state


class DummyChat:
    def __init__(self, chat_id=1, chat_type="private"):
        self.id = chat_id
        self.type = chat_type


class DummyMessage:
    def __init__(self, text="", chat_id=1):
        self.text = text
        self.chat = DummyChat(chat_id)

    async def answer(self, *args, **kwargs):
        return None

    async def edit_reply_markup(self, *args, **kwargs):
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
async def test_cb_activity_select_sets_level_and_moves_to_goal(monkeypatch):
    state = DummyState()

    called = {"ask_goal": False, "fired": False}

    async def fake_ask_goal(message):
        called["ask_goal"] = True

    def fake_fire_event(evt):
        called["fired"] = True

    monkeypatch.setattr(onboarding_mod, "_ask_goal", fake_ask_goal)
    # analytics.fire_event is referenced via imported symbol in module scope
    monkeypatch.setattr(onboarding_mod.analytics, "logger", True, raising=False)
    monkeypatch.setattr(onboarding_mod.analytics, "fire_event", fake_fire_event, raising=False)

    call = DummyCall(data="activity:moderate", user_id=42)

    await onboarding_mod.cb_activity_select(call, state)

    data = await state.get_data()
    assert data.get("activity_level") == "moderate"
    # State should be moved to goal
    st = await state.get_state()
    # OnboardingStates.goal is a State; module uses set_state(OnboardingStates.goal)
    # We accept either object equality or stringy value
    assert st == onboarding_mod.OnboardingStates.goal or st == onboarding_mod.OnboardingStates.goal.state
    assert called["ask_goal"] is True
    assert called["fired"] is True


@pytest.mark.asyncio
async def test_height_set_shows_activity_buttons(monkeypatch):
    # Replace _ask_activity to capture call
    called = {"ask_activity": False}

    async def fake_ask_activity(message):
        called["ask_activity"] = True

    monkeypatch.setattr(onboarding_mod, "_ask_activity", fake_ask_activity)

    state = DummyState()
    msg = DummyMessage(text="177")

    await onboarding_mod.height_set(msg, state)

    # Verify we switched to activity state and asked activity
    st = await state.get_state()
    assert st == onboarding_mod.OnboardingStates.activity or st == onboarding_mod.OnboardingStates.activity.state
    assert called["ask_activity"] is True


def test_calculate_plan_no_text_uses_level():
    data = OnboardingData(
        user_id=1,
        gender=Gender.male,
        age=30,
        weight_kg=80.0,
        height_cm=180.0,
        activity_text=None,
        activity_level=ActivityLevel.active,
        goal=Goal.maintain,
        goal_weight_kg=None,
        speed=None,
    )
    plan = calculate_daily_plan(data)
    # For maintain, target calories should equal TDEE = BMR * multiplier
    # BMR (Mifflin): 10*80 + 6.25*180 - 5*30 + 5 = 800 + 1125 - 150 + 5 = 1780
    # Multiplier for active = 1.725
    expected_tdee = int(round(1780 * 1.725))
    assert plan.tdee == expected_tdee
    assert plan.calories == expected_tdee


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "sedentary",
    "light",
    "moderate",
    "active",
    "athlete",
])
async def test_cb_activity_select_all_levels(monkeypatch, code):
    state = DummyState()

    called = {"ask_goal": False, "fired": False}

    async def fake_ask_goal(message):
        called["ask_goal"] = True

    def fake_fire_event(evt):
        called["fired"] = True

    monkeypatch.setattr(onboarding_mod, "_ask_goal", fake_ask_goal)
    monkeypatch.setattr(onboarding_mod.analytics, "logger", True, raising=False)
    monkeypatch.setattr(onboarding_mod.analytics, "fire_event", fake_fire_event, raising=False)

    call = DummyCall(data=f"activity:{code}", user_id=101)

    await onboarding_mod.cb_activity_select(call, state)

    data = await state.get_data()
    assert data.get("activity_level") == code
    st = await state.get_state()
    assert st == onboarding_mod.OnboardingStates.goal or st == onboarding_mod.OnboardingStates.goal.state
    assert called["ask_goal"] is True
    assert called["fired"] is True
