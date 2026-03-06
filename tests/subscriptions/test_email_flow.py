from __future__ import annotations
from typing import NoReturn

import pytest
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import UserModel
from bot.handlers import onboarding as ob


class _DummyMessage:
    def __init__(self, uid: int) -> None:
        self.last_text: str | None = None
        self.from_user = type("U", (), {"id": uid, "language_code": "ru"})()

    async def edit_text(self, text: str, reply_markup=None, disable_web_page_preview: bool | None = None) -> None:
        self.last_text = text

    async def answer(self, text: str, reply_markup=None, disable_web_page_preview: bool | None = None) -> None:
        self.last_text = text


class _DummyCall:
    def __init__(self, uid: int) -> None:
        self.from_user = type("U", (), {"id": uid, "language_code": "ru"})()
        self.message = _DummyMessage(uid)
        self.data = ""

    async def answer(self) -> None:
        return None


class _FakeFSM:
    def __init__(self) -> None:
        self._state = None
        self._data: dict = {}

    async def set_state(self, state) -> None:  # state object
        self._state = state

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def clear(self) -> None:
        self._state = None
        self._data = {}

    @property
    def state(self):
        return self._state


@pytest.mark.asyncio
async def test_sale_pay_month_without_email_prompts_and_sets_state(test_db_env, ensure_user, monkeypatch) -> None:
    uid = await ensure_user(10101, email=None)
    call = _DummyCall(uid)
    fsm = _FakeFSM()

    def _raise_email_required(*args, **kwargs) -> NoReturn:
        raise RuntimeError("email_required")

    monkeypatch.setattr(ob, "create_payment", _raise_email_required, raising=True)

    await ob.sale_pay_month(call, state=fsm)  # type: ignore[arg-type]

    assert fsm.state == ob.EmailStates.waiting
    data = await fsm.get_data()
    assert data.get("pay_plan") == "month"
    assert "e-mail" in (call.message.last_text or "")


@pytest.mark.asyncio
async def test_email_capture_rejects_invalid_email_and_keeps_state(test_db_env, ensure_user) -> None:
    uid = await ensure_user(10102, email=None)
    fsm = _FakeFSM()
    await fsm.set_state(ob.EmailStates.waiting)
    await fsm.update_data(pay_plan="month")

    msg = _DummyMessage(uid)
    msg.text = "not-an-email"

    await ob.email_capture(msg, state=fsm)  # type: ignore[arg-type]

    assert "valid email" in (msg.last_text or "")
    assert fsm.state == ob.EmailStates.waiting


@pytest.mark.asyncio
async def test_email_capture_accepts_valid_email_saves_and_continues_payment(test_db_env, ensure_user, monkeypatch) -> None:
    uid = await ensure_user(10103, email=None)
    fsm = _FakeFSM()
    await fsm.set_state(ob.EmailStates.waiting)
    await fsm.update_data(pay_plan="month")

    class _CP:
        confirmation_url = "https://example/pay"
        payment_id = "pay_x"
        idempotence_key = "idem"

    async def _fake_create_payment(user_id: int, plan: str, **kwargs):
        assert user_id == uid
        assert plan == "month"
        return _CP()

    monkeypatch.setattr(ob, "create_payment", _fake_create_payment, raising=True)

    msg = _DummyMessage(uid)
    msg.text = "buyer@example.com"

    await ob.email_capture(msg, state=fsm)  # type: ignore[arg-type]

    async with sessionmaker() as session:
        row = (await session.execute(select(UserModel).where(UserModel.id == uid))).scalar_one_or_none()
        assert row is not None
        assert row.email == "buyer@example.com"

    assert (msg.last_text or "").startswith("Proceed to payment")
    assert fsm.state is None
    assert (await fsm.get_data()) == {}
