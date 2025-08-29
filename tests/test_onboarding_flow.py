from __future__ import annotations

import pytest
from sqlalchemy import select


class DummyUser:
    def __init__(self, user_id: int = 123):
        self.id = user_id
        self.first_name = "Test"
        self.last_name = None
        self.username = None
        self.language_code = "ru"


class DummyMessage:
    def __init__(self, user_id: int = 123) -> None:
        self.from_user = DummyUser(user_id)
        self.captured: list[tuple[str, dict]] = []
        self.chat = type("_Chat", (), {"id": 1, "type": "private"})()

    async def answer(self, text: str, reply_markup=None, **kwargs) -> None:  # type: ignore[override]
        self.captured.append(("answer", {"text": text, "reply_markup": reply_markup, **kwargs}))

    async def answer_photo(self, photo, caption: str | None = None, reply_markup=None, **kwargs) -> None:
        self.captured.append(("answer_photo", {"caption": caption, "reply_markup": reply_markup, **kwargs}))


class DummyCallback:
    def __init__(self, user_id: int = 123):
        self.from_user = DummyUser(user_id)
        self.message = DummyMessage(user_id)
        self.data: str | None = None
        self.answered: bool = False

    async def answer(self) -> None:
        self.answered = True


class DummyState:
    def __init__(self, state: str | None = None) -> None:
        self._state = state
        self._data: dict = {}

    async def set_state(self, state) -> None:  # aiogram FSMContext compat subset
        # store raw state object/string
        self._state = state

    async def get_state(self):  # aiogram FSMContext compat subset
        return self._state

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def clear(self) -> None:
        self._state = None
        self._data.clear()


@pytest.mark.asyncio
async def test_onboarding_happy_path_lose(apply_migrations, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics network calls
    from bot.services import analytics as analytics_module

    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    # Import handlers after env/migrations are ready
    from bot.handlers import onboarding as ob
    from bot.database.models import OnboardingAnswerModel, UserModel

    # Avoid aiogram I18n context by replacing _ with identity
    monkeypatch.setattr(ob, "_", lambda s: s)

    user_id = 10011
    # Ensure user exists (AuthMiddleware is not active in direct handler calls)
    await db_session.merge(
        UserModel(
            id=user_id,
            first_name="Test",
            last_name=None,
            username=None,
            language_code="ru",
            referrer=None,
        )
    )
    await db_session.commit()
    state = DummyState()

    # Start via callback onboarding_start
    call = DummyCallback(user_id)
    call.data = "onboarding_start"
    await ob.cb_onboarding_start(call, state)  # sets state to gender
    cur = await state.get_state()
    assert cur is not None

    # Select gender
    call.data = "gender:male"
    await ob.cb_gender(call, state)
    assert await state.get_state() is not None  # age

    # Age
    msg = DummyMessage(user_id)
    msg.text = "35"  # type: ignore[attr-defined]
    await ob.age_set(msg, state)

    # Weight
    msg = DummyMessage(user_id)
    msg.text = "85.0"  # type: ignore[attr-defined]
    await ob.weight_set(msg, state)

    # Height
    msg = DummyMessage(user_id)
    msg.text = "180"  # type: ignore[attr-defined]
    await ob.height_set(msg, state)

    # Activity
    msg = DummyMessage(user_id)
    msg.text = "Хожу 8-10к шагов, 2 тренировки, иногда бег."  # type: ignore[attr-defined]
    await ob.activity_set(msg, state)

    # Goal -> lose
    call = DummyCallback(user_id)
    call.data = "goal:lose"
    await ob.cb_goal(call, state)

    # Goal weight (less than current)
    msg = DummyMessage(user_id)
    msg.text = "75.0"  # type: ignore[attr-defined]
    await ob.goal_weight_set(msg, state)

    # Speed -> finalize
    call = DummyCallback(user_id)
    call.data = "speed:COMFORT"
    await ob.cb_speed(call, state)

    # After finalize, state should be review
    cur = await state.get_state()
    assert cur is not None

    # Verify DB record exists
    rec = await db_session.scalar(
        select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
    )
    assert rec is not None
    assert rec.daily_plan is not None
    assert rec.goal in ("lose", "gain", "maintain")
    assert isinstance(rec.calories, int) and rec.calories > 0


@pytest.mark.asyncio
async def test_onboarding_maintain_direct_finalize(apply_migrations, db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics network calls
    from bot.services import analytics as analytics_module

    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    from bot.handlers import onboarding as ob
    from bot.database.models import OnboardingAnswerModel, UserModel

    monkeypatch.setattr(ob, "_", lambda s: s)

    user_id = 10012
    # Ensure user exists (AuthMiddleware is not active in direct handler calls)
    await db_session.merge(
        UserModel(
            id=user_id,
            first_name="Test",
            last_name=None,
            username=None,
            language_code="ru",
            referrer=None,
        )
    )
    await db_session.commit()
    state = DummyState()

    # Minimal path to goal selection
    call = DummyCallback(user_id)
    call.data = "onboarding_start"
    await ob.cb_onboarding_start(call, state)

    call.data = "gender:male"
    await ob.cb_gender(call, state)

    msg = DummyMessage(user_id)
    msg.text = "30"  # type: ignore[attr-defined]
    await ob.age_set(msg, state)

    msg = DummyMessage(user_id)
    msg.text = "70.0"  # type: ignore[attr-defined]
    await ob.weight_set(msg, state)

    msg = DummyMessage(user_id)
    msg.text = "175"  # type: ignore[attr-defined]
    await ob.height_set(msg, state)

    msg = DummyMessage(user_id)
    msg.text = "Сижу много, 2 раза спорт в неделю."  # type: ignore[attr-defined]
    await ob.activity_set(msg, state)

    # maintain should trigger immediate finalize
    call = DummyCallback(user_id)
    call.data = "goal:maintain"
    await ob.cb_goal(call, state)

    # After finalize, state should be review
    cur = await state.get_state()
    assert cur is not None

    rec = await db_session.scalar(
        select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id)
    )
    assert rec is not None
    assert rec.goal == "maintain"
    assert isinstance(rec.calories, int) and rec.calories > 0
