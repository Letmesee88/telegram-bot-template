from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import NoReturn

import pytest
from sqlalchemy import delete, select, update

from bot.database.database import sessionmaker
from bot.database.models import PaymentModel, SubscriptionModel, UserModel


@pytest.fixture(autouse=True)
async def _cleanup_db() -> None:
    # Isolate tests: remove prior subscriptions/payments
    async with sessionmaker() as session:
        await session.execute(delete(PaymentModel))
        await session.execute(delete(SubscriptionModel))
        await session.commit()


@pytest.mark.asyncio
async def test_scheduler_initial_submit_success(test_db_env, ensure_user, fake_bot, monkeypatch) -> None:
    # Arrange user and subscription due now
    user_id = await ensure_user(10031)
    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc) - timedelta(days=60),
            # Make it due regardless of hour gate (previous date)
            expires_at_utc=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    # Patch YooKassa SDK call used by service to avoid network and let DB persist pending payment
    import bot.services.yookassa as yk

    class _Obj:
        id = "pay_rebill_1"

    def _fake_payment_create(payload, idempotency_key=None):
        return _Obj()

    monkeypatch.setattr(yk.Payment, "create", _fake_payment_create, raising=True)

    # Act: call _try_rebill directly
    # Align redis client used inside scheduler with test fake
    import bot.background.recurring_scheduler as rs
    from bot.background.recurring_scheduler import _period_key, _RebillTask, _try_rebill
    from bot.core.loader import redis_client as _fake_redis
    monkeypatch.setattr(rs, "redis_client", _fake_redis, raising=True)
    period = _period_key(datetime.now(timezone.utc))
    task = _RebillTask(
        subscription_id=sub_id,
        user_id=user_id,
        plan="month",
        payment_method_id="pm_saved",
        period_key=period,
    )
    await _try_rebill(fake_bot, task)

    # Assert: Payment record exists pending and linked meta, and submitted key set via redis (implicitly by service)
    async with sessionmaker() as session:
        pm = (await session.execute(select(PaymentModel).where(PaymentModel.user_id == user_id).order_by(PaymentModel.id.desc()))).scalars().first()
        assert pm is not None
        assert pm.status == "pending"
        assert pm.subscription_id == sub_id
        assert pm.meta
        assert pm.meta.get("rebill") is True
        assert pm.meta.get("subscription_id") == sub_id


@pytest.mark.asyncio
async def test_scheduler_initial_failure_schedules_retry_and_closes_access(test_db_env, ensure_user, fake_bot, monkeypatch) -> None:
    user_id = await ensure_user(10032)
    async with sessionmaker() as session:
        # Ensure premium on, to verify it gets disabled
        await session.execute(update(UserModel).where(UserModel.id == user_id).values(is_premium=True))
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="year",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc) - timedelta(days=400),
            # Make it due regardless of hour gate (previous date)
            expires_at_utc=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    # Force YooKassa SDK create to raise to simulate immediate failure
    import bot.services.yookassa as yk

    def _raise_payment_create(payload, idempotency_key=None) -> NoReturn:
        msg = "test fail"
        raise RuntimeError(msg)

    monkeypatch.setattr(yk.Payment, "create", _raise_payment_create, raising=True)

    # Align redis client used inside scheduler with test fake
    import bot.background.recurring_scheduler as rs
    from bot.background.recurring_scheduler import ZSET_DUE, _period_key, _RebillTask, _try_rebill
    from bot.core.loader import redis_client as _fake_redis
    monkeypatch.setattr(rs, "redis_client", _fake_redis, raising=True)
    from bot.core.loader import redis_client

    period = _period_key(datetime.now(timezone.utc))
    task = _RebillTask(
        subscription_id=sub_id,
        user_id=user_id,
        plan="year",
        payment_method_id="pm_saved",
        period_key=period,
    )
    await _try_rebill(fake_bot, task)

    # Check subscription past_due and premium disabled
    async with sessionmaker() as session:
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id))).scalar_one_or_none()
        assert sub is not None
        assert sub.status == "past_due"
        u = (await session.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one_or_none()
        assert u is not None
        assert not bool(u.is_premium)

    # Check retry scheduled in ZSET_DUE
    # We expect a member like f"{sub_id}:{YYYY-MM-DD}"
    members = await redis_client.zrangebyscore(ZSET_DUE, min="-inf", max=int(datetime.now(timezone.utc).timestamp()) + 86400)
    assert any(str(m).startswith(f"{sub_id}:") for m in members)


@pytest.mark.asyncio
@pytest.mark.xfail(strict=False, reason="Hour gate needs stable wall-clock mocking; to be stabilized later")
async def test_scheduler_respects_local_hour_gate(test_db_env, ensure_user, fake_bot, monkeypatch) -> None:
    user_id = await ensure_user(10033)
    # Force user's local time < 10 to block attempt (set expiry early in day + freeze now that morning)
    import bot.background.recurring_scheduler as rs

    async def _fake_tzinfo_block(session, uid):
        return timezone(timedelta(hours=0))

    class _FDT2:
        @classmethod
        def now(cls, tz=None):
            base = datetime(2025, 1, 1, 6, 0, tzinfo=timezone.utc)
            return base if tz is None else base.astimezone(tz)

    monkeypatch.setattr(rs, "get_user_tzinfo", _fake_tzinfo_block, raising=True)
    monkeypatch.setattr(rs, "datetime", _FDT2, raising=True)

    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            payment_method_id="pm_saved",
            auto_renew=True,
            # expires early in the day to keep "due" while before 10:00 local
            started_at_utc=datetime(2024, 12, 1, 0, 0, tzinfo=timezone.utc),
            expires_at_utc=datetime(2025, 1, 1, 5, 30, tzinfo=timezone.utc),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)

    # We override tz via service stub above; no need to touch DB timezone

    # Spy: stub YooKassa create to count calls; should be 0 (blocked by gate)
    called = {"count": 0}

    import bot.services.yookassa as yk

    def _spy_payment_create(payload, idempotency_key=None) -> NoReturn:
        called["count"] += 1
        msg = "should not be called before gate hour"
        raise RuntimeError(msg)

    monkeypatch.setattr(yk.Payment, "create", _spy_payment_create, raising=True)

    from bot.background.recurring_scheduler import RecurringScheduler

    sch = RecurringScheduler()
    sch._bot = fake_bot  # type: ignore[attr-defined]
    await sch._scan_and_submit_initial()

    assert called["count"] == 0
