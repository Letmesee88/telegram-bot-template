from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.schemas.onboarding import ActivityLevel, Gender, Goal, OnboardingData
from bot.services.plan import calculate_daily_plan


class DummyUser:
    def __init__(self, user_id: int = 123) -> None:
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
        # optional attributes used in handlers
        from datetime import datetime
        self.date = datetime.utcnow()

    async def answer(self, text: str, reply_markup=None, **kwargs) -> None:  # type: ignore[override]
        self.captured.append(("answer", {"text": text, "reply_markup": reply_markup, **kwargs}))

    async def answer_photo(self, photo, caption: str | None = None, reply_markup=None, **kwargs) -> None:
        self.captured.append(("answer_photo", {"caption": caption, "reply_markup": reply_markup, **kwargs}))


class DummyCallback:
    def __init__(self, user_id: int = 123) -> None:
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


class FakeRedis:
    def __init__(self) -> None:
        self.storage: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.storage.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.storage[key] = value.encode("utf-8") if isinstance(value, str) else value

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.storage.pop(key, None)


@pytest.mark.asyncio
async def test_settings_kb_two_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import settings as s
    kb = s._kb_settings()
    rows = kb.inline_keyboard
    assert len(rows) >= 2
    assert len(rows[0]) == 2
    assert len(rows[1]) == 2
    assert rows[0][0].text == "📊 Суточная норма"
    assert rows[0][1].text == "📌 Шаблоны блюд"


@pytest.mark.asyncio
async def test_daily_norm_open_shows_values(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics
    from bot.services import analytics as analytics_module
    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    from bot.database.database import sessionmaker
    from bot.database.models import OnboardingAnswerModel, UserModel
    from bot.handlers import settings as s
    from bot.schemas.onboarding import ActivityLevel, Gender, Goal, OnboardingData
    from bot.services.plan import calculate_daily_plan

    # neutralize i18n _
    monkeypatch.setattr(s, "_", lambda x: x)

    user_id = 11001
    async with sessionmaker() as session:
        await session.merge(UserModel(id=user_id, first_name="T", last_name=None, username=None, language_code="ru"))
        await session.commit()

    # Create onboarding record with full payload and plan
    payload = OnboardingData(
        user_id=user_id,
        gender=Gender.male,
        age=30,
        weight_kg=90.0,
        height_cm=180.0,
        activity_text=None,
        activity_level=ActivityLevel.light,
        goal=Goal.lose,
        goal_weight_kg=80.0,
        speed=None,
    )
    plan_obj = calculate_daily_plan(payload)
    plan = plan_obj.model_dump(mode="json")
    data = payload.model_dump(mode="json")
    async with sessionmaker() as session:
        existing = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        if existing:
            existing.data = data
            existing.daily_plan = plan
            existing.goal = "lose"
            existing.calories = 2000
        else:
            session.add(OnboardingAnswerModel(user_id=user_id, data=data, daily_plan=plan, goal="lose", calories=2000))
        await session.commit()

    call = DummyCallback(user_id)
    call.data = "settings:open:daily_norm"
    await s.cb_settings_open_daily_norm(call)

    # Check last message text (do not assert exact numbers, plan is computed)
    assert call.message.captured, "no messages sent"
    txt = call.message.captured[-1][1]["text"]
    assert "Калории:" in txt
    assert "Белки:" in txt
    assert "Жиры:" in txt
    assert "Углеводы:" in txt
    assert "Цель:" in txt
    assert "До цели:" in txt


@pytest.mark.asyncio
async def test_daily_norm_edit_start_sets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import settings as s
    monkeypatch.setattr(s, "_", lambda x: x)
    call = DummyCallback(12001)
    state = DummyState()
    call.data = "daily_norm:edit_start"
    await s.cb_daily_norm_edit_start(call, state)
    assert await state.get_state() is not None
    assert call.message.captured, "no message rendered"
    txt = call.message.captured[-1][1]["text"]
    assert "что нужно скорректировать" in txt


@pytest.mark.asyncio
async def test_daily_norm_apply_updates_and_invalidate_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics
    from bot.services import analytics as analytics_module
    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    from bot.database.models import OnboardingAnswerModel, UserModel
    from bot.handlers import settings as s

    monkeypatch.setattr(s, "_", lambda x: x)

    # Fake Redis
    fake = FakeRedis()
    monkeypatch.setattr(s, "redis_client", fake)

    from bot.database.database import sessionmaker
    user_id = 13001
    async with sessionmaker() as session:
        await session.merge(UserModel(id=user_id, first_name="T", last_name=None, username=None, language_code="ru"))
        await session.commit()

    payload = OnboardingData(
        user_id=user_id,
        gender=Gender.male,
        age=30,
        weight_kg=90.0,
        height_cm=180.0,
        activity_text=None,
        activity_level=ActivityLevel.light,
        goal=Goal.lose,
        goal_weight_kg=80.0,
        speed=None,
    )
    base_plan_obj = calculate_daily_plan(payload)
    base_plan = base_plan_obj.model_dump(mode="json")
    data = payload.model_dump(mode="json") | {"activity_level": payload.activity_level.value}
    async with sessionmaker() as session:
        existing = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        if existing:
            existing.data = data
            existing.daily_plan = base_plan
            existing.goal = "lose"
            existing.calories = 2000
        else:
            session.add(OnboardingAnswerModel(user_id=user_id, data=data, daily_plan=base_plan, goal="lose", calories=2000))
        await session.commit()

    # Mock parse + apply
    class Parsed:
        intents = ["high_protein"]
        rationale = "сделать акцент на белок"
        activity_override = None
        calories = None
        macros = {"protein_g": 160}
        confidence = 0.9

    async def fake_parse(user_id_arg, text, **kwargs):
        return Parsed()

    def fake_apply(base_plan_obj, payload, parsed_obj):
        from bot.schemas.onboarding import DailyPlan
        # build a complete DailyPlan to satisfy validation
        new = DailyPlan(
            calories=2100,
            protein_g=160,
            fat_g=70,
            carbs_g=190,
            sources=list(getattr(base_plan_obj, "sources", []) or []),
            tdee=int(getattr(base_plan_obj, "tdee", 0) or 0),
            weekly_rate_kg=float(getattr(base_plan_obj, "weekly_rate_kg", 0.0) or 0.0),
            eta_date=getattr(base_plan_obj, "eta_date", None),
        )
        return new, "Детальное объяснение…", {"dummy": True}

    monkeypatch.setattr(s, "parse_adjustment_cached", fake_parse)
    monkeypatch.setattr(s, "apply_adjustment", fake_apply)

    msg = DummyMessage(user_id)
    msg.text = "подними белок"  # type: ignore[attr-defined]
    state = DummyState()

    await s.daily_norm_adjust_apply(msg, state)

    # DB updated
    async with sessionmaker() as session:
        rec = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
    assert rec is not None
    assert int(rec.calories) == 2100
    assert int(rec.daily_plan.get("protein_g")) == 160

    # Redis invalidation
    assert f"account:summary:{user_id}" in fake.deleted

    # Message content
    assert msg.captured, "no response message"
    out = msg.captured[-1][1]["text"]
    assert out.startswith("<b>Твой план скорректирован!")
    assert "Обновленная дневная норма:" in out
    assert "Калории: 2100" in out
    assert "Белки: 160" in out
    assert "Оставим так или нужна еще корректировка?" in out


@pytest.mark.asyncio
async def test_daily_norm_llm_only_rephrase_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics
    from bot.services import analytics as analytics_module
    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    from bot.database.models import OnboardingAnswerModel, UserModel
    from bot.handlers import settings as s

    monkeypatch.setattr(s, "_", lambda x: x)

    # Force llm_only
    monkeypatch.setattr(s.settings, "ADJUST_ENGINE_MODE", "llm_only")

    from bot.database.database import sessionmaker
    user_id = 14001
    async with sessionmaker() as session:
        await session.merge(UserModel(id=user_id, first_name="T", last_name=None, username=None, language_code="ru"))
        await session.commit()

    payload = OnboardingData(
        user_id=user_id,
        gender=Gender.male,
        age=30,
        weight_kg=90.0,
        height_cm=180.0,
        activity_text=None,
        activity_level=ActivityLevel.light,
        goal=Goal.lose,
        goal_weight_kg=80.0,
        speed=None,
    )
    plan_obj = calculate_daily_plan(payload)
    plan = plan_obj.model_dump(mode="json")
    data = payload.model_dump(mode="json") | {"activity_level": payload.activity_level.value}
    async with sessionmaker() as session:
        existing = await session.scalar(select(OnboardingAnswerModel).where(OnboardingAnswerModel.user_id == user_id))
        if existing:
            existing.data = data
            existing.daily_plan = plan
            existing.goal = "lose"
            existing.calories = int(plan.get("calories") or 0)
        else:
            session.add(OnboardingAnswerModel(user_id=user_id, data=data, daily_plan=plan, goal="lose", calories=int(plan.get("calories") or 0)))
        await session.commit()

    async def parse_none(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(s, "parse_adjustment_cached", parse_none)

    msg = DummyMessage(user_id)
    msg.text = "что-то расплывчатое"  # type: ignore[attr-defined]
    state = DummyState()

    await s.daily_norm_adjust_apply(msg, state)

    assert msg.captured, "no response message"
    text = msg.captured[-1][1]["text"]
    assert "Не до конца понял запрос" in text
    # state should not be cleared; user remains in waiting_text stage
    assert await state.get_state() is None or await state.get_state() == getattr(s.SettingsDailyNormStates, "waiting_text", None)
