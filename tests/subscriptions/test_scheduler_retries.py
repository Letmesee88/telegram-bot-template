from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from bot.database.database import sessionmaker
from bot.database.models import SubscriptionModel, UserModel


@pytest.mark.asyncio
async def test_process_due_retries_triggers_try_rebill_once_and_consumes_due(
    test_db_env, ensure_user, monkeypatch, fake_bot
) -> None:
    user_id = await ensure_user(10071)
    # Seed subscription with saved PM
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

    # Ensure scheduler uses fake redis
    import bot.background.recurring_scheduler as rs
    from bot.core.loader import redis_client as _fake_redis
    monkeypatch.setattr(rs, "redis_client", _fake_redis, raising=True)

    # Patch Payment.create to count calls
    import bot.services.yookassa as yk
    calls = {"n": 0}

    class _Obj:
        id = "pay_retry_1"

    def _spy_payment_create(payload, idempotency_key=None):
        calls["n"] += 1
        return _Obj()

    monkeypatch.setattr(yk.Payment, "create", _spy_payment_create, raising=True)

    # Enqueue due item
    period = datetime.now(timezone.utc).date().isoformat()
    await _fake_redis.zadd(rs.ZSET_DUE, {f"{sub_id}:{period}": int(datetime.now(timezone.utc).timestamp())})

    sch = rs.RecurringScheduler()
    sch._bot = fake_bot  # type: ignore[attr-defined]
    await sch._process_due_retries()

    assert calls["n"] == 1
    # Due entry consumed
    left = await _fake_redis.zrangebyscore(rs.ZSET_DUE, min="-inf", max=9999999999)
    assert all(not str(m).startswith(f"{sub_id}:{period}") for m in left)

    # If SUBMITTED is present, subsequent due should not call again
    await _fake_redis.zadd(rs.ZSET_DUE, {f"{sub_id}:{period}": int(datetime.now(timezone.utc).timestamp())})
    await _fake_redis.set(f"rebill:submitted:{sub_id}:{period}", "1")
    await sch._process_due_retries()

    assert calls["n"] == 1  # unchanged


@pytest.mark.asyncio
async def test_try_rebill_missing_payment_method_sets_past_due_and_notifies_once(
    test_db_env, ensure_user, fake_bot, capture_bot_messages
) -> None:
    user_id = await ensure_user(10072)
    # Seed subscription with missing PM
    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            payment_method_id=None,
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc) - timedelta(days=30),
            expires_at_utc=datetime.now(timezone.utc) - timedelta(days=1),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    import bot.background.recurring_scheduler as rs
    period = rs._period_key(datetime.now(timezone.utc))
    task = rs._RebillTask(
        subscription_id=sub_id,
        user_id=user_id,
        plan="month",
        payment_method_id=None,
        period_key=period,
    )

    await rs._try_rebill(fake_bot, task)  # type: ignore[arg-type]

    # Assert past_due and notification exactly once
    async with sessionmaker() as session:
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id))).scalar_one_or_none()
        assert sub is not None
        assert sub.status == "past_due"
        u = (await session.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one_or_none()
        assert u is not None
        assert not bool(u.is_premium)
    assert len(capture_bot_messages) == 1
