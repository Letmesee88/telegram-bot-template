from __future__ import annotations

import pytest

from bot.services.foodai import refine_meal

# ==============================
# Unit tests for refine_meal()
# ==============================

@pytest.mark.asyncio
async def test_refine_meal_add_grams() -> None:
    base = {
        "title": "Салат",
        "calories": 200,
        "protein_g": 10.0,
        "fat_g": 8.0,
        "carbs_g": 20.0,
        "weight_g": 250.0,
        "items": [
            {"name": "огурец", "weight_g": 100.0, "calories": 15, "protein_g": 0.7, "fat_g": 0.1, "carbs_g": 3.0},
        ],
    }
    out = await refine_meal(base, "добавить сыр 30 г")
    assert isinstance(out, dict)
    assert not out.get("error")
    assert out.get("meta", {}).get("action") == "add"
    items = out.get("items") or []
    assert any("сыр" in (i.get("name") or "").lower() for i in items)
    # Сыр ~330 ккал/100г => ~99 ккал добавки. Итого > базовых 200
    assert int(out.get("calories") or 0) > 200


@pytest.mark.asyncio
async def test_refine_meal_remove_not_found() -> None:
    base = {"title": "Блюдо", "calories": 100, "protein_g": 5.0, "fat_g": 3.0, "carbs_g": 10.0, "weight_g": 150.0, "items": []}
    out = await refine_meal(base, "убрать соус")
    assert out.get("error") == "not_found"
    assert out.get("meta", {}).get("reason") == "not_found"


@pytest.mark.asyncio
async def test_refine_meal_replace_ambiguous() -> None:
    base = {
        "title": "Рыба с рисом",
        "calories": 400,
        "protein_g": 25.0,
        "fat_g": 10.0,
        "carbs_g": 50.0,
        "weight_g": 350.0,
        "items": [
            {"name": "рыба", "weight_g": 120.0, "calories": 220, "protein_g": 20.0, "fat_g": 10.0, "carbs_g": 0.0},
            {"name": "рыба копченая", "weight_g": 30.0, "calories": 60, "protein_g": 6.0, "fat_g": 3.0, "carbs_g": 0.0},
        ],
    }
    out = await refine_meal(base, "заменить рыба на индейка 100 г")
    assert out.get("error") == "ambiguous"
    assert out.get("meta", {}).get("action") == "replace"


@pytest.mark.asyncio
async def test_refine_meal_scale_percent() -> None:
    base = {"title": "Блюдо", "calories": 300, "protein_g": 15.0, "fat_g": 10.0, "carbs_g": 30.0, "weight_g": 300.0, "items": [
        {"name": "рис", "weight_g": 200.0, "calories": 220, "protein_g": 4.0, "fat_g": 1.5, "carbs_g": 48.0},
    ]}
    out = await refine_meal(base, "+20%")
    assert not out.get("error")
    assert out.get("meta", {}).get("action") == "scale"
    assert float(out.get("weight_g") or 0) >= 360.0 - 0.1  # 300 * 1.2


@pytest.mark.asyncio
async def test_refine_meal_change_qty_ml() -> None:
    base = {
        "title": "Салат",
        "calories": 150,
        "protein_g": 6.0,
        "fat_g": 7.0,
        "carbs_g": 12.0,
        "weight_g": 220.0,
        "items": [
            {"name": "масло", "weight_g": 10.0, "calories": 90, "protein_g": 0.0, "fat_g": 10.0, "carbs_g": 0.0},
        ],
    }
    out = await refine_meal(base, "масло 5 мл")
    assert not out.get("error")
    assert out.get("meta", {}).get("action") == "change_qty"
    items = out.get("items") or []
    assert any(abs((i.get("weight_g") or 0) - 5.0 * 0.91) < 0.001 for i in items if (i.get("name") or "").lower().startswith("масло"))


# ==============================
# Integration-ish: handler flow
# ==============================

@pytest.mark.asyncio
async def test_edit_flow_add_success(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable analytics
    from bot.services import analytics as analytics_module
    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    # Import handler lazily to ensure DB env is ready
    from bot.handlers import foodai as h
    monkeypatch.setattr(h, "_", lambda s: s)

    # Create meal in DB
    from bot.database.models import MealModel, UserModel
    # Ensure user exists to satisfy FK
    await db_session.merge(UserModel(id=5001, first_name="T", last_name=None, username=None, language_code="ru"))
    await db_session.commit()
    m = MealModel(user_id=5001, title="Салат", calories=200, protein_g=10.0, fat_g=8.0, carbs_g=20.0, weight_g=250.0, status="draft", source="edit")
    db_session.add(m)
    await db_session.commit()

    # Prepare state and message
    class _State:
        def __init__(self) -> None:
            self._data = {"edit_meal_id": m.id}
        async def get_data(self):
            return dict(self._data)
        async def clear(self) -> None:
            self._data.clear()

    class _Chat:
        id = 1
        type = "private"

    class _User:
        id = 5001
        language_code = "ru"

    class _Msg:
        def __init__(self) -> None:
            self.from_user = _User()
            self.chat = _Chat()
            self.text = "добавить сыр 30 г"
            self.sent: list[str] = []
        async def answer(self, text: str, **kwargs) -> None:
            self.sent.append(text)

    state = _State()
    msg = _Msg()

    await h.edit_text_received(msg, state)

    # Two messages should be sent: confirmation and preview
    assert any("Готово" in s for s in msg.sent)

    # DB should be updated: meal items contain сыр
    # Reload from DB via new session
    from bot.database.database import sessionmaker as sm
    async with sm() as s:
        mm = await s.get(MealModel, m.id)
        assert mm is not None
        names = [it.name for it in (mm.items or [])]
        assert any("сыр" in (n or "").lower() for n in names)


@pytest.mark.asyncio
async def test_edit_flow_error_message(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.services import analytics as analytics_module
    monkeypatch.setattr(analytics_module.analytics, "logger", None)

    from bot.handlers import foodai as h
    monkeypatch.setattr(h, "_", lambda s: s)

    # Create meal in DB
    from bot.database.models import MealModel, UserModel
    await db_session.merge(UserModel(id=5002, first_name="T", last_name=None, username=None, language_code="ru"))
    await db_session.commit()
    m = MealModel(user_id=5002, title="Блюдо", calories=100, protein_g=5.0, fat_g=3.0, carbs_g=10.0, weight_g=150.0, status="draft", source="edit")
    db_session.add(m)
    await db_session.commit()

    class _State:
        def __init__(self) -> None:
            self._data = {"edit_meal_id": m.id}
        async def get_data(self):
            return dict(self._data)
        async def clear(self) -> None:
            self._data.clear()

    class _Chat:
        id = 1
        type = "private"

    class _User:
        id = 5002
        language_code = "ru"

    class _Msg:
        def __init__(self) -> None:
            self.from_user = _User()
            self.chat = _Chat()
            self.text = "блаблабла"  # unsupported
            self.sent: list[str] = []
        async def answer(self, text: str, **kwargs) -> None:
            self.sent.append(text)

    state = _State()
    msg = _Msg()

    await h.edit_text_received(msg, state)

    # Should respond with generic failure or mapped reason text
    assert any("Не удалось" in s or "Пока не поддерживаю" in s for s in msg.sent)
