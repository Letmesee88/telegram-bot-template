# tests/test_foodai_fsm_isolation.py
from __future__ import annotations
import asyncio
import types
from typing import NoReturn


class DummyState:
    def __init__(self, value: str | None) -> None:
        self._value = value

    async def get_state(self):
        return self._value


class DummyMessage:
    def __init__(self, text: str | None = None, photo: list | None = None) -> None:
        # Provide only attributes potentially accessed by handler AFTER guard
        self.text = text
        self.photo = photo or []
        self.from_user = types.SimpleNamespace(id=123)
        self.chat = types.SimpleNamespace(id=456, type="private")

    async def answer(self, *args, **kwargs) -> NoReturn:  # should not be called in these tests
        msg = "answer() should not be called when FSM is active"
        raise AssertionError(msg)

    async def answer_photo(self, *args, **kwargs) -> NoReturn:
        msg = "answer_photo() should not be called when FSM is active"
        raise AssertionError(msg)


class DummyCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = types.SimpleNamespace(id=123)
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=456, type="private"))
        self._answered = False

    async def answer(self, *args, **kwargs) -> None:
        # In handler we do a silent acknowledge before returning
        self._answered = True


def test_foodai_text_ignored_when_fsm_active(monkeypatch) -> None:
    async def _run() -> None:
        from bot.handlers import foodai as foodai_mod

        # If analyze_text is called -> fail the test
        async def explode(*_, **__) -> NoReturn:
            msg = "analyze_text must NOT be called when FSM is active"
            raise AssertionError(msg)

        monkeypatch.setattr(foodai_mod, "analyze_text", explode)

        msg = DummyMessage(text="кофе")
        state = DummyState(value="onboarding:age")

        # Should return early with no side effects
        await foodai_mod.handle_food_text(msg, state)  # type: ignore[arg-type]

    asyncio.run(_run())


def test_foodai_photo_ignored_when_fsm_active(monkeypatch) -> None:
    async def _run() -> None:
        from bot.handlers import foodai as foodai_mod

        async def explode(*_, **__) -> NoReturn:
            msg = "analyze_photo must NOT be called when FSM is active"
            raise AssertionError(msg)

        monkeypatch.setattr(foodai_mod, "analyze_photo", explode)

        # Even if photo exists, handler must early-return under FSM
        photo_size = types.SimpleNamespace(file_id="id", file_unique_id="uid", width=1, height=1)
        msg = DummyMessage(text=None, photo=[photo_size])
        state = DummyState(value="onboarding:gender")

        await foodai_mod.handle_food_photo(msg, state)  # type: ignore[arg-type]

    asyncio.run(_run())


def test_foodai_callback_ignored_when_fsm_active() -> None:
    async def _run() -> None:
        from bot.handlers import foodai as foodai_mod

        cb = DummyCallback(data="foodai:save:1")
        state = DummyState(value="onboarding:review")

        await foodai_mod.cb_foodai_save(cb, state)  # type: ignore[arg-type]
        assert cb._answered is True, "Callback should be acknowledged and ignored under FSM"

    asyncio.run(_run())
