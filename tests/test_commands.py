import pytest

from bot.handlers.start import cmd_add_meal, cmd_support
from bot.keyboards.default_commands import users_commands


class FakeChat:
    def __init__(self, id=1, type="private") -> None:
        self.id = id
        self.type = type


class FakeFromUser:
    def __init__(self, id=123, language_code="ru") -> None:
        self.id = id
        self.language_code = language_code


class FakeMessage:
    def __init__(self, user_id=123) -> None:
        self.from_user = FakeFromUser(user_id)
        self.chat = FakeChat(777, "private")
        self._answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self._answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_cmd_add_meal_text_and_no_cta() -> None:
    m = FakeMessage(user_id=1)
    await cmd_add_meal(m)
    assert len(m._answers) == 1
    txt = m._answers[0][0]
    # Key fragments must be present
    assert "Как добавить блюдо" in txt
    assert "Сфотографируйте блюдо" in txt
    assert "опишите его словами" in txt
    # No CTA
    assert "подписка не активна" not in txt.lower()


@pytest.mark.asyncio
async def test_cmd_support_text_and_no_cta() -> None:
    m = FakeMessage(user_id=2)
    await cmd_support(m)
    assert len(m._answers) == 1
    txt = m._answers[0][0]
    assert "Поддержка  пользователей" in txt
    assert "@Tonya_19_93" in txt
    assert "подписка не активна" not in txt.lower()


def test_ru_commands_order_and_presence() -> None:
    ru = users_commands.get("ru", {})
    expected_order = [
        "account",
        "templates",
        "day",
        "history",
        "add_meal",
        "support",
        "start",
    ]
    assert list(ru.keys()) == expected_order
    # Spot-check descriptions
    assert ru["add_meal"].startswith("❓")
    assert ru["support"].startswith("☕️")
    assert ru["start"].startswith("🚀")
