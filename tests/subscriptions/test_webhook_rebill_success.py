from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import PaymentModel, SubscriptionModel


@pytest.mark.asyncio
async def test_rebill_succeeded_applies_next_plan_and_nonotify(
    test_db_env, ensure_user, yk_stub, make_webhook_request, make_yk_view, capture_bot_messages
) -> None:
    user_id = await ensure_user(10061)

    # Seed current subscription with next_plan to be applied on rebill
    async with sessionmaker() as session:
        now = datetime.now(timezone.utc)
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            next_plan="year",
            payment_method_id="pm_saved_1",
            started_at_utc=now - timedelta(days=30),
            expires_at_utc=now - timedelta(minutes=1),  # due
        )
        session.add(sub)
        await session.commit()

    # Stub YooKassa: succeeded rebill for year
    amount = f"{settings.PRICE_YEAR_RUB:.2f}"
    metadata = {"user_id": user_id, "plan": "year", "rebill": True, "subscription_id": 1, "period_key": "2025-01-01"}
    yk_stub(status="succeeded", value=amount, metadata=metadata, pm_id="pm_saved_1", pm_saved=True)

    req = make_webhook_request({"event": "payment.succeeded", "object": {"id": "pay_rebill_ok_1"}})
    view = make_yk_view(req)
    resp = await view.post()
    assert getattr(resp, "text", "OK") == "OK"

    # Assert subscription updated to next_plan and next_plan cleared, no notification sent
    async with sessionmaker() as session:
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))).scalar_one_or_none()
        assert sub is not None
        assert sub.plan == "year"
        assert sub.next_plan is None
        assert sub.expires_at_utc is not None
        assert sub.expires_at_utc > datetime.now(timezone.utc)
        # Payment stored succeeded
        pm = (await session.execute(select(PaymentModel).where(PaymentModel.yk_payment_id == "pay_rebill_ok_1"))).scalar_one_or_none()
        assert pm is not None
        assert pm.status == "succeeded"

    # Nonotify on rebill success
    assert len(capture_bot_messages) == 0


@pytest.mark.asyncio
async def test_payment_succeeded_amount_mismatch_ignored(
    test_db_env, ensure_user, yk_stub, make_webhook_request, make_yk_view
) -> None:
    user_id = await ensure_user(10062)

    # Stub YooKassa: succeeded month but with wrong amount value
    wrong_amount = f"{settings.PRICE_MONTH_RUB + 1:.2f}"
    metadata = {"user_id": user_id, "plan": "month"}
    yk_stub(status="succeeded", value=wrong_amount, metadata=metadata, pm_id="pm_irrelevant", pm_saved=True)

    req = make_webhook_request({"event": "payment.succeeded", "object": {"id": "pay_bad_amount"}})
    view = make_yk_view(req)
    resp = await view.post()
    assert getattr(resp, "text", "OK") == "OK"

    # Assert no payment/ subscription changes
    async with sessionmaker() as session:
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))).scalar_one_or_none()
        assert sub is None
        pm = (await session.execute(select(PaymentModel).where(PaymentModel.yk_payment_id == "pay_bad_amount"))).scalar_one_or_none()
        assert pm is None
