from __future__ import annotations

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database.models import SubscriptionModel


@pytest.mark.asyncio
async def test_scheduler_past_due_notification_has_pay_button(async_sessionmaker, ensure_user) -> None:
    from bot.background.recurring_scheduler import _mark_past_due_and_notify

    user_id = await ensure_user(10021)

    # Create an active monthly subscription
    async with async_sessionmaker() as session:
        s = SubscriptionModel(user_id=user_id, status="active", plan="month")
        session.add(s)
        await session.commit()
        await session.refresh(s)
        sub_id = s.id

    # Capture outgoing message including reply_markup
    sent: list[dict] = []

    class _Bot:
        async def send_message(self, uid: int, text: str, **kwargs) -> None:
            sent.append({"user_id": uid, "text": text, "kwargs": kwargs})

    bot = _Bot()

    # Act
    await _mark_past_due_and_notify(bot, sub_id=sub_id, user_id=user_id)

    # Assert message sent with keyboard
    assert sent, "No message sent by scheduler notification"
    msg = sent[-1]
    kb = msg["kwargs"].get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup), "reply_markup must be InlineKeyboardMarkup"
    # Single row, single button expected
    assert kb.inline_keyboard
    assert kb.inline_keyboard[0]
    assert isinstance(kb.inline_keyboard[0][0], InlineKeyboardButton)
    btn = kb.inline_keyboard[0][0]
    assert btn.callback_data == "sale:pay:month"


@pytest.mark.asyncio
async def test_webhook_rebill_canceled_has_pay_button(async_sessionmaker, ensure_user, make_webhook_request, make_yk_view, monkeypatch) -> None:
    from bot.core.loader import bot as real_bot

    user_id = await ensure_user(10022)

    # Create active yearly subscription (to test year button)
    async with async_sessionmaker() as session:
        s = SubscriptionModel(user_id=user_id, status="active", plan="year")
        session.add(s)
        await session.commit()
        await session.refresh(s)
        sub_id = s.id

    # Patch bot.send_message to capture reply_markup
    sent: list[dict] = []

    async def _fake_send_message(uid: int, text: str, **kwargs) -> None:
        sent.append({"user_id": uid, "text": text, "kwargs": kwargs})

    monkeypatch.setattr(real_bot, "send_message", _fake_send_message, raising=True)

    # Build canceled webhook payload for rebill
    payload = {
        "event": "payment.canceled",
        "object": {
            "id": "PAY_TEST_ID",
            "metadata": {
                "user_id": user_id,
                "rebill": True,
                "subscription_id": sub_id,
                "period_key": "2025-12-17",
            },
            "cancellation_details": {},
        },
    }

    req = make_webhook_request(payload)
    view = make_yk_view(req)

    # Act
    await view.post()

    # Assert message and keyboard
    assert sent, "No message sent by webhook cancellation handler"
    kb = sent[-1]["kwargs"].get("reply_markup")
    assert isinstance(kb, InlineKeyboardMarkup), "reply_markup must be InlineKeyboardMarkup"
    assert kb.inline_keyboard
    assert kb.inline_keyboard[0]
    btn = kb.inline_keyboard[0][0]
    assert isinstance(btn, InlineKeyboardButton)
    assert btn.callback_data == "sale:pay:year"
