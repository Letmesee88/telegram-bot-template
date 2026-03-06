import importlib
import types

import pytest

from bot.handlers import templates as tpl_mod
from bot.keyboards.templates import categories_browse_kb, templates_list_kb


@pytest.fixture(autouse=True)
def _patch_i18n(monkeypatch) -> None:
    # Neutralize aiogram i18n in unit tests
    templates_mod = importlib.import_module("bot.handlers.templates")
    foodai_mod = importlib.import_module("bot.handlers.foodai")
    monkeypatch.setattr(templates_mod, "_", lambda s: s, raising=False)
    monkeypatch.setattr(foodai_mod, "_", lambda s: s, raising=False)
    # Also patch aiogram i18n context globally
    monkeypatch.setattr(
        "aiogram.utils.i18n.context.get_i18n",
        lambda no_error=False: types.SimpleNamespace(gettext=lambda s, **kwargs: s),
        raising=False,
    )

class DummyUser:
    def __init__(self, user_id: int, language_code: str = "ru") -> None:
        self.id = user_id
        self.language_code = language_code


class DummyChat:
    def __init__(self, chat_id: int = 1, chat_type: str = "private") -> None:
        self.id = chat_id
        self.type = chat_type


class DummyMessage:
    def __init__(self) -> None:
        self.last_text = None
        self.last_markup = None
        self.chat = DummyChat()

    async def edit_text(self, text: str, reply_markup=None) -> None:
        self.last_text = text
        self.last_markup = reply_markup

    async def answer(self, text: str, reply_markup=None) -> None:
        # Fallback path
        self.last_text = text
        self.last_markup = reply_markup


class DummyCallback:
    def __init__(self, data: str, user_id: int = 123) -> None:
        self.data = data
        self.from_user = DummyUser(user_id)
        self.message = DummyMessage()

    async def answer(self, *args, **kwargs) -> None:
        return None


# -------- Pure keyboard tests --------

def test_categories_browse_kb_two_columns(monkeypatch) -> None:
    # Patch i18n gettext to no-op to avoid aiogram I18n context
    monkeypatch.setattr("bot.keyboards.templates._", lambda s: s, raising=False)
    kb = categories_browse_kb({"breakfast": 1, "lunch": 2, "dinner": 3, "snack": 4})
    rows = kb.inline_keyboard
    # Expect 2 rows, each up to 2 buttons
    assert len(rows) == 2
    assert all(1 <= len(r) <= 2 for r in rows)
    # First row: breakfast, lunch
    assert rows[0][0].text.startswith("🥞 ")
    assert rows[0][1].text.startswith("🍜 ")


def test_templates_list_kb_buttons_symbols(monkeypatch) -> None:
    monkeypatch.setattr("bot.keyboards.templates._", lambda s: s, raising=False)
    data = [(1, "Блюдо 1"), (2, "Блюдо 2"), (3, "Блюдо 3")]
    kb = templates_list_kb(data, "lunch")
    rows = kb.inline_keyboard
    # First three rows are pairs of [➕ #i][❌Удалить]
    assert rows[0][0].text.startswith("➕ #1")
    assert rows[0][1].text == "❌Удалить"
    assert rows[1][0].text.startswith("➕ #2")
    assert rows[1][1].text == "❌Удалить"


# -------- Handler tests with monkeypatches --------

class _FakeMeal:
    def __init__(self, user_id=123, title="Блины", calories=558, p=18.9, f=32.8, c=50.5, w=340.0, source="photo") -> None:
        self.user_id = user_id
        self.title = title
        self.calories = calories
        self.protein_g = p
        self.fat_g = f
        self.carbs_g = c
        self.weight_g = w
        self.confidence = 0.8
        self.source = source
        self.items = [
            types.SimpleNamespace(name="драник картофельный (крупный)", weight_g=200, calories=360),
            types.SimpleNamespace(name="яйцо пашот", weight_g=50, calories=72),
        ]
        self.references = None


class _FakeDI:
    def __init__(self, cal=0, p=0, f=0, c=0) -> None:
        self.calories = cal
        self.protein_g = p
        self.fat_g = f
        self.carbs_g = c


class _FakeOA:
    def __init__(self) -> None:
        self.daily_plan = {"calories": 2000, "protein_g": 120.0, "fat_g": 70.0, "carbs_g": 250.0}


class _FakeSession:
    def __init__(self, meal: _FakeMeal | None = None, di: _FakeDI | None = None, oa: _FakeOA | None = None) -> None:
        self._meal = meal
        self._di = di
        self._oa = oa

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, obj_id):
        # Return meal for MealModel
        if model is tpl_mod.MealModel:
            return self._meal
        return None

    async def scalar(self, stmt):
        # Very naive router by model name in SQL string repr
        txt = str(stmt)
        if "daily_intake" in txt or "DailyIntake" in txt:
            return self._di
        if "onboarding" in txt or "Onboarding" in txt:
            return self._oa
        return None


class _FakeSessionMaker:
    def __init__(self, session) -> None:
        self._session = session

    def __call__(self):
        return self._session


async def _async_noop(*args, **kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_cb_tpl_del_confirmation(monkeypatch) -> None:
    cb = DummyCallback("tpl:del:1:lunch")
    # Patch delete_template to no-op at the handler import site
    templates_mod = importlib.import_module("bot.handlers.templates")
    services_mod = importlib.import_module("bot.services.templates")
    monkeypatch.setattr(templates_mod, "delete_template", _async_noop, raising=False)
    monkeypatch.setattr(services_mod, "delete_template", _async_noop, raising=False)
    # Use sessionmaker that just enters/exits
    fake_sess = _FakeSession()
    monkeypatch.setattr(tpl_mod, "sessionmaker", _FakeSessionMaker(fake_sess), raising=False)

    await tpl_mod.cb_tpl_del(cb)
    assert "❌ Шаблон удалён" in cb.message.last_text
    assert "Вернуться к списку: /templates" in cb.message.last_text


@pytest.mark.asyncio
async def test_cb_tpl_save_back_returns_saved_with_day_analysis(monkeypatch) -> None:
    cb = DummyCallback("tpl:save_back:10")
    meal = _FakeMeal(user_id=cb.from_user.id)
    di = _FakeDI(cal=1000, p=50, f=30, c=120)
    oa = _FakeOA()
    fake_sess = _FakeSession(meal=meal, di=di, oa=oa)
    monkeypatch.setattr(tpl_mod, "sessionmaker", _FakeSessionMaker(fake_sess), raising=False)
    monkeypatch.setattr("bot.handlers.templates._", lambda s: s, raising=False)
    monkeypatch.setattr("bot.handlers.foodai._", lambda s: s, raising=False)

    await tpl_mod.cb_tpl_save_back(cb)
    text = cb.message.last_text or ""
    assert text.startswith("✅ Еда сохранена")
    assert "Анализ дня:" in text
    assert "Калории:" in text
    assert "Белки:" in text
    assert "Жиры:" in text
    assert "Углеводы:" in text


@pytest.mark.asyncio
async def test_cb_tpl_add_preview_includes_grams_and_kcal(monkeypatch) -> None:
    cb = DummyCallback("tpl:add:21")
    meal = _FakeMeal(user_id=cb.from_user.id)
    fake_sess = _FakeSession(meal=meal)
    monkeypatch.setattr(tpl_mod, "sessionmaker", _FakeSessionMaker(fake_sess), raising=False)
    # Patch create_meal_draft_from_template to return a fixed meal_id
    async def _fake_create(*args, **kwargs) -> int:
        return 777
    monkeypatch.setattr(tpl_mod, "create_meal_draft_from_template", _fake_create, raising=False)
    monkeypatch.setattr("bot.handlers.templates._", lambda s: s, raising=False)
    monkeypatch.setattr("bot.handlers.foodai._", lambda s: s, raising=False)

    await tpl_mod.cb_tpl_add(cb)
    text = cb.message.last_text or ""
    # Must contain composition header and item lines with grams and kcal
    assert "🍜 Состав:" in text
    assert "200 г" in text
    assert "360 ккал" in text
    # Totals and weight present
    assert "🔥 Калории:" in text
    assert "⚖️ Вес:" in text


@pytest.mark.asyncio
async def test_cb_tpl_save_entry_formatting_and_spacing(monkeypatch) -> None:
    cb = DummyCallback("tpl:save:10")
    meal = _FakeMeal(user_id=cb.from_user.id)
    fake_sess = _FakeSession(meal=meal)
    monkeypatch.setattr(tpl_mod, "sessionmaker", _FakeSessionMaker(fake_sess), raising=False)
    monkeypatch.setattr("bot.handlers.templates._", lambda s: s, raising=False)
    monkeypatch.setattr("bot.handlers.foodai._", lambda s: s, raising=False)

    await tpl_mod.cb_tpl_save_entry(cb)
    text = cb.message.last_text or ""
    # Header and subheader present
    assert text.startswith("📌Создаю шаблон\nОн будет доступен через команду /templates\n")
    # Exactly one blank line before bold title
    lines = text.splitlines()
    # lines[0] header, lines[1] subheader, lines[2] should be empty, lines[3] title
    assert len(lines) >= 4
    assert lines[2] == ""
    assert lines[3].startswith("<b>")
