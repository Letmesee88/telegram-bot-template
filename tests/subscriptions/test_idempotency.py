from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import SubscriptionModel, PaymentModel, UserModel


@pytest.mark.asyncio
async def test_webhook_idempotent_success_duplicate_event(
    test_db_env, ensure_user, yk_stub, make_webhook_request, make_yk_view
):
    user_id = await ensure_user(10041)
    # Stub YooKassa find_one to return succeeded month with saved PM
    amount = f"{settings.PRICE_MONTH_RUB:.2f}"
    metadata = {"user_id": user_id, "plan": "month"}
    yk_stub(status="succeeded", value=amount, metadata=metadata, pm_id="pm_idem_1", pm_saved=True)

    payload = {
        "event": "payment.succeeded",
        "object": {"id": "pay_dup_1"},
    }
    req = make_webhook_request(payload)
    view = make_yk_view(req)

    # First delivery
    resp1 = await view.post()
    assert getattr(resp1, "text", "OK") == "OK"

    # Snapshot subscription expiry after first success
    async with sessionmaker() as session:
        sub = (
            await session.execute(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        ).scalar_one_or_none()
        assert sub is not None
        exp1 = sub.expires_at_utc

    # Duplicate delivery (idempotent)
    req2 = make_webhook_request(payload)
    view2 = make_yk_view(req2)
    resp2 = await view2.post()
    assert getattr(resp2, "text", "OK") == "OK"

    # Assert: only one payment row with that yk_payment_id; expiry unchanged
    async with sessionmaker() as session:
        payments = (
            await session.execute(
                select(PaymentModel).where(PaymentModel.yk_payment_id == "pay_dup_1").order_by(PaymentModel.id.asc())
            )
        ).scalars().all()
        assert len(payments) == 1
        sub2 = (
            await session.execute(select(SubscriptionModel).where(SubscriptionModel.user_id == user_id))
        ).scalar_one_or_none()
        assert sub2 is not None
        assert sub2.expires_at_utc == exp1


@pytest.mark.asyncio
async def test_scheduler_idempotent_submitted_key_prevents_duplicate_submit(
    test_db_env, ensure_user, monkeypatch
):
    user_id = await ensure_user(10042)
    # Prepare subscription
    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc) - timedelta(days=30),
            expires_at_utc=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    # Spy on YooKassa SDK call
    import bot.services.yookassa as yk

    calls = {"n": 0}

    class _Obj:
        id = "pay_idem_1"

    def _spy_payment_create(payload, idempotency_key=None):
        calls["n"] += 1
        return _Obj()

    monkeypatch.setattr(yk.Payment, "create", _spy_payment_create, raising=True)

    # Ensure scheduler uses same fake redis instance
    import bot.background.recurring_scheduler as rs
    from bot.core.loader import redis_client as _fake_redis
    monkeypatch.setattr(rs, "redis_client", _fake_redis, raising=True)

    # Call _try_rebill twice with same (sub, period)
    from bot.background.recurring_scheduler import _try_rebill, _RebillTask, _period_key

    period = _period_key(datetime.now(timezone.utc))
    task = _RebillTask(
        subscription_id=sub_id,
        user_id=user_id,
        plan="month",
        payment_method_id="pm_saved",
        period_key=period,
    )

    # First submit
    from bot.core.loader import bot as real_bot
    await _try_rebill(real_bot, task)
    # Second submit (should be no-op due to SUBMITTED key)
    await _try_rebill(real_bot, task)

    assert calls["n"] == 1

    # Only one pending payment exists
    async with sessionmaker() as session:
        pms = (
            await session.execute(
                select(PaymentModel).where(
                    PaymentModel.user_id == user_id,
                    PaymentModel.subscription_id == sub_id,
                ).order_by(PaymentModel.id.asc())
            )
        ).scalars().all()
        assert len(pms) == 1
