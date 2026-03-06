from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import InlineKeyboardMarkup


class DummyUser:
    def __init__(self, user_id: int = 123) -> None:
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


class DummyCBMessage:
    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    async def edit_caption(self, caption: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:  # type: ignore[override]
        # Simulate text edit path via caption first
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

    async def answer(self, *args, **kwargs) -> None:
        return None


class _DT(datetime):
    @classmethod
    def now(cls, tz=None):
        # Fixed date to make assertions deterministic
        base = datetime(2025, 1, 2, 12, 0)
        return base.replace(tzinfo=tz)


class _FakeResult:
    def __init__(self, meals: list[object]) -> None:
        self._meals = meals

    class _Scalars:
        def __init__(self, meals: list[object]) -> None:
            self._meals = meals

        def all(self):
            return list(self._meals)

    def scalars(self):
        return _FakeResult._Scalars(self._meals)


class _FakeSession:
    def __init__(self, meals: list[object], oa_obj: object | None) -> None:
        self._meals = meals
        self._oa = oa_obj

    async def execute(self, _query):
        return _FakeResult(self._meals)

    async def scalar(self, _query):
        return self._oa


class _FakeSM:
    def __init__(self, meals: list[object], oa_obj: object | None) -> None:
        self._meals = meals
        self._oa = oa_obj

    async def __aenter__(self):
        return _FakeSession(self._meals, self._oa)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMeal:
    def __init__(self, title: str, dt: datetime, cal=0, p=0.0, f=0.0, c=0.0) -> None:
        self.title = title
        self.consumed_at = dt
        self.calories = cal
        self.protein_g = p
        self.fat_g = f
        self.carbs_g = c


class FakeOA:
    def __init__(self, plan: dict | None = None) -> None:
        self.daily_plan = plan or {"calories": 0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}


@pytest.mark.asyncio
async def test_cmd_day_empty_shows_zero_stats_no_meals_no_nav(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    # i18n identity
    monkeypatch.setattr(menu_module, "_", lambda s: s)
    # Fixed tz and time
    async def _fake_tz(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    # No meals, zero plan
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM([], FakeOA()))

    msg = DummyMessage(user_id=777)
    await menu_module.cmd_day(msg)  # type: ignore[arg-type]

    # Validate text
    text = msg.captured.get("text")
    assert isinstance(text, str)
    assert "Дневник за" in text
    assert "📈 Общая статистика:" in text
    assert "Вы съели:" not in text  # section is omitted when no meals
    # Zero progress values present
    assert "0.0 %" in text

    # Validate keyboard: no navigation
    kb = msg.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    # Only the edit button (no navigation)
    assert "✏️ Изменить блюда" in texts
    assert "◀️ Назад" not in texts
    assert "Вперёд ▶️" not in texts
    assert "de:l:1" in datas
    assert not any(d.startswith("diary:today:") for d in datas)


@pytest.mark.asyncio
async def test_cmd_day_first_page_shows_forward_label(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    async def _fake_tz(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    # 12 meals within the fixed day -> 2 pages
    meals = [
        FakeMeal(title=f"Meal {i+1}", dt=datetime(2025, 1, 2, 8 + (i % 10), 0, tzinfo=timezone.utc)) for i in range(12)
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals, FakeOA()))

    msg = DummyMessage(user_id=777)
    await menu_module.cmd_day(msg)  # type: ignore[arg-type]

    kb = msg.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]

    assert "Вперёд ▶️" in texts
    assert "diary:today:2" in datas


@pytest.mark.asyncio
async def test_cb_diary_today_middle_page_shows_back_and_forward(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    async def _fake_tz2(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz2)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    # 25 meals -> 3 pages (PAGE_SIZE=10)
    meals = [
        FakeMeal(title=f"Meal {i+1}", dt=datetime(2025, 1, 2, (i % 24), 0, tzinfo=timezone.utc)) for i in range(25)
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals, FakeOA()))

    cb = DummyCallback(data="diary:today:2", user_id=777)
    await menu_module.cb_diary_today(cb)  # type: ignore[arg-type]

    # Text updated
    text = cb.message.captured.get("text")
    assert isinstance(text, str)
    assert "Дневник за" in text
    assert "Вы съели:" in text

    # Keyboard has both Back and Forward
    kb = cb.message.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]

    assert "◀️ Назад" in texts
    assert "Вперёд ▶️" in texts
    assert f"diary:today:{1}" in datas  # back
    assert f"diary:today:{3}" in datas  # forward


@pytest.mark.asyncio
async def test_cb_diary_today_last_page_shows_back_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    async def _fake_tz(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    # 21 meals -> 3 pages; check page 3
    meals = [
        FakeMeal(title=f"Meal {i+1}", dt=datetime(2025, 1, 2, (i % 24), 0, tzinfo=timezone.utc)) for i in range(21)
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals, FakeOA()))

    cb = DummyCallback(data="diary:today:3", user_id=777)
    await menu_module.cb_diary_today(cb)  # type: ignore[arg-type]

    kb = cb.message.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    assert "✏️ Изменить блюда" in texts
    assert "◀️ Назад" in texts
    assert "Вперёд ▶️" not in texts
    assert "de:l:1" in datas
    assert "diary:today:2" in datas


@pytest.mark.asyncio
async def test_cmd_day_exactly_10_meals_no_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    async def _fake_tz(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    meals = [
        FakeMeal(title=f"Meal {i+1}", dt=datetime(2025, 1, 2, (8 + i) % 24, 0, tzinfo=timezone.utc)) for i in range(10)
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals, FakeOA()))

    msg = DummyMessage(user_id=777)
    await menu_module.cmd_day(msg)  # type: ignore[arg-type]

    kb = msg.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    # Only the edit button (no navigation on single full page)
    assert "✏️ Изменить блюда" in texts
    assert "◀️ Назад" not in texts
    assert "Вперёд ▶️" not in texts
    assert "de:l:1" in datas
    assert not any(d.startswith("diary:today:") for d in datas)


@pytest.mark.asyncio
async def test_cmd_day_exactly_20_meals_two_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    async def _fake_tz(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    meals = [
        FakeMeal(title=f"Meal {i+1}", dt=datetime(2025, 1, 2, (i % 24), 0, tzinfo=timezone.utc)) for i in range(20)
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals, FakeOA()))

    msg = DummyMessage(user_id=777)
    await menu_module.cmd_day(msg)  # type: ignore[arg-type]

    kb = msg.captured.get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    assert "✏️ Изменить блюда" in texts
    assert "Вперёд ▶️" in texts
    assert "◀️ Назад" not in texts
    assert "de:l:1" in datas
    assert "diary:today:2" in datas

    # Page 2 should have only back
    cb = DummyCallback(data="diary:today:2", user_id=777)
    await menu_module.cb_diary_today(cb)  # type: ignore[arg-type]
    kb2 = cb.message.captured.get("reply_markup")
    assert isinstance(kb2, InlineKeyboardMarkup)
    buttons2 = [btn for row in kb2.inline_keyboard for btn in row]
    texts2 = [btn.text for btn in buttons2]
    datas2 = [btn.callback_data for btn in buttons2]
    assert "✏️ Изменить блюда" in texts2
    assert "◀️ Назад" in texts2
    assert "Вперёд ▶️" not in texts2
    assert "de:l:1" in datas2
    assert "diary:today:1" in datas2


@pytest.mark.asyncio
async def test_cmd_day_timezone_dst_boundary_berlin(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    berlin = ZoneInfo("Europe/Berlin")
    async def _fake_tz(_session, _uid):
        return berlin
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)

    class _DT_DST(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2025, 3, 30, 12, 0)
            return base.replace(tzinfo=tz)
    monkeypatch.setattr(menu_module, "datetime", _DT_DST)

    # Create one meal at 23:30 local previous day -> 22:30 UTC previous day (should be excluded by DB)
    prev_local = datetime(2025, 3, 29, 23, 30, tzinfo=berlin)
    prev_local.astimezone(timezone.utc)
    # Create one meal at 00:05 local current day -> 23:05 UTC previous day (included)
    cur_local = datetime(2025, 3, 30, 0, 5, tzinfo=berlin)
    cur_utc = cur_local.astimezone(timezone.utc)

    # Our FakeSession doesn't apply SQL filters, so we simulate DB returning already-filtered rows.
    meals_in_range = [
        FakeMeal(title="M2", dt=cur_utc, cal=150),
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals_in_range, FakeOA()))

    msg = DummyMessage(user_id=777)
    await menu_module.cmd_day(msg)  # type: ignore[arg-type]

    text = msg.captured.get("text")
    assert isinstance(text, str)
    # Only M2 should be listed
    assert "M2" in text
    assert "M1" not in text
    # Time should be formatted in local Berlin time as 00:05
    assert "(00:05)" in text


@pytest.mark.asyncio
async def test_cmd_day_nonzero_plan_percentages_and_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    async def _fake_tz(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    # Totals: 1000 kcal, 50/25/125 g
    meals = [
        FakeMeal("A", dt=datetime(2025, 1, 2, 8, 0, tzinfo=timezone.utc), cal=600, p=30, f=10, c=60),
        FakeMeal("B", dt=datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc), cal=400, p=20, f=15, c=65),
    ]
    plan = {"calories": 2000, "protein_g": 100.0, "fat_g": 50.0, "carbs_g": 250.0}
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals, FakeOA(plan)))

    msg = DummyMessage(user_id=777)
    await menu_module.cmd_day(msg)  # type: ignore[arg-type]

    text = msg.captured.get("text")
    assert isinstance(text, str)
    # Check percentages ~ 50.0 %
    assert "50.0 %" in text
    # Progress bars should contain 5 green blocks in at least one line
    assert "🟩🟩🟩🟩🟩" in text


@pytest.mark.asyncio
async def test_cmd_day_title_fallback_and_time_format(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.handlers import menu as menu_module

    monkeypatch.setattr(menu_module, "_", lambda s: s)
    async def _fake_tz(_session, _uid):
        return timezone.utc
    monkeypatch.setattr(menu_module, "get_user_tzinfo", _fake_tz)
    monkeypatch.setattr(menu_module, "datetime", _DT)

    # Title fallback: None or whitespace -> "Блюдо"; ensure time format HH:MM
    meals = [
        FakeMeal(title=None, dt=datetime(2025, 1, 2, 8, 0, tzinfo=timezone.utc), cal=100),  # type: ignore[arg-type]
        FakeMeal(title="   ", dt=datetime(2025, 1, 2, 9, 5, tzinfo=timezone.utc), cal=100),
    ]
    monkeypatch.setattr(menu_module, "sessionmaker", lambda: _FakeSM(meals, FakeOA()))

    msg = DummyMessage(user_id=777)
    await menu_module.cmd_day(msg)  # type: ignore[arg-type]

    text = msg.captured.get("text")
    assert isinstance(text, str)
    # Fallback title appears
    assert "Блюдо" in text
    # Time formatting
    assert "(08:00)" in text
    assert "(09:05)" in text
