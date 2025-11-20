from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import SubscriptionModel, PaymentModel, UserModel


@pytest.mark.asyncio
async def test_payment_succeeded_trial_pm_saved_true(test_db_env, ensure_user, yk_stub, make_webhook_request, make_yk_view, capture_bot_messages):
    user_id = await ensure_user(10011)
    # Stub YooKassa find_one to return succeeded trial with saved PM
    amount = f"{settings.PRICE_TRIAL_RUB:.2f}"
    metadata = {"user_id": user_id, "plan": "trial"}
    yk_stub(status="succeeded", value=amount, metadata=metadata, pm_id="pm_test_1", pm_saved=True)

    req = make_webhook_request({
        "event": "payment.succeeded",
        "object": {"id": "pay_1"},
    })
    view = make_yk_view(req)
    resp = await view.post()
    assert getattr(resp, "text", "OK") == "OK"

    async with sessionmaker() as session:
        # Payment stored as succeeded
        pm = (await session.execute(select(PaymentModel).where(PaymentModel.yk_payment_id == "pay_1"))).scalar_one_or_none()
        assert pm is not None and pm.status == "succeeded"
        # Subscription created/updated
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))).scalar_one_or_none()
        assert sub is not None
        # plan trial, payment_method_id saved
        assert sub.plan == "trial"
        assert sub.payment_method_id == "pm_test_1"
        assert sub.status == "active"
        assert sub.expires_at_utc is not None and sub.expires_at_utc > datetime.now(timezone.utc) - timedelta(seconds=5)
        # User premium enabled
        u = (await session.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one_or_none()
        assert u is not None and bool(u.is_premium)


@pytest.mark.asyncio
async def test_payment_succeeded_pm_not_saved(test_db_env, ensure_user, yk_stub, make_webhook_request, make_yk_view):
    user_id = await ensure_user(10012)
    # Stub YooKassa find_one to return succeeded month with NOT saved PM
    amount = f"{settings.PRICE_MONTH_RUB:.2f}"
    metadata = {"user_id": user_id, "plan": "month"}
    yk_stub(status="succeeded", value=amount, metadata=metadata, pm_id="pm_test_2", pm_saved=False)

    req = make_webhook_request({
        "event": "payment.succeeded",
        "object": {"id": "pay_2"},
    })
    view = make_yk_view(req)
    _ = await view.post()

    async with sessionmaker() as session:
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))).scalar_one_or_none()
        assert sub is not None
        assert sub.plan == "month"
        # payment_method_id must NOT be stored when saved == False
        assert sub.payment_method_id is None
