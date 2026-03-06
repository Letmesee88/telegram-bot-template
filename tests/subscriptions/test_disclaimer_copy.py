from __future__ import annotations

import pytest

from bot.handlers.onboarding import sale_buy_month, sale_buy_year, sale_trial


class _DummyMessage:
    def __init__(self) -> None:
        self.last_text: str | None = None

    async def edit_text(self, text: str, reply_markup=None, disable_web_page_preview: bool | None = None) -> None:
        self.last_text = text

    async def answer(self, text: str, reply_markup=None, disable_web_page_preview: bool | None = None) -> None:
        self.last_text = text


class _DummyFromUser:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.language_code = "ru"


class _DummyCall:
    def __init__(self, uid: int) -> None:
        self.from_user = _DummyFromUser(uid)
        self.message = _DummyMessage()
        self.data = ""

    async def answer(self) -> None:
        return None


@pytest.mark.asyncio
async def test_trial_screen_contains_free_trial_copy(test_db_env, ensure_user) -> None:
    uid = await ensure_user(10051)
    call = _DummyCall(uid)

    await sale_trial(call, state=None)  # type: ignore[arg-type]

    text = call.message.last_text or ""
    assert "Free trial" in text
    assert "no cost" in text
    assert "monthly or yearly plan" in text


@pytest.mark.asyncio
async def test_month_screen_contains_plan_copy(test_db_env, ensure_user) -> None:
    uid = await ensure_user(10052)
    call = _DummyCall(uid)

    await sale_buy_month(call, state=None)  # type: ignore[arg-type]

    text = call.message.last_text or ""
    assert "Plan: Monthly" in text
    assert "Price: 750 RUB / month" in text
    assert "renews automatically" in text


@pytest.mark.asyncio
async def test_year_screen_contains_plan_copy(test_db_env, ensure_user) -> None:
    uid = await ensure_user(10053)
    call = _DummyCall(uid)

    await sale_buy_year(call, state=None)  # type: ignore[arg-type]

    text = call.message.last_text or ""
    assert "Plan: Yearly" in text
    assert "Price: 2500 RUB / year" in text
    assert "renews automatically" in text
