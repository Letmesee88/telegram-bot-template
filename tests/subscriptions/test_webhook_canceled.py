from __future__ import annotations

import pytest
from sqlalchemy import select
from datetime import datetime, timezone

from bot.database.database import sessionmaker
from bot.database.models import SubscriptionModel, UserModel


@pytest.mark.asyncio
async def test_rebill_canceled_temporary_reason(test_db_env, ensure_user, make_webhook_request, make_yk_view, patch_redis_client, monkeypatch):
    user_id = await ensure_user(10021)
    # Seed subscription
    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc),
            expires_at_utc=datetime.now(timezone.utc),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    # Send canceled webhook for rebill with temporary reason (issuer_unavailable)
    # Ensure webhook uses the same fake redis instance as loader
    import bot.handlers.yookassa_webhook as wh
    from bot.core.loader import redis_client as _fake_redis
    monkeypatch.setattr(wh, "redis_client", _fake_redis, raising=True)

    payload = {
        "event": "payment.canceled",
        "object": {
            "id": "pay_tmp_1",
            "metadata": {
                "user_id": user_id,
                "rebill": True,
                "subscription_id": sub_id,
                "period_key": datetime.now(timezone.utc).date().isoformat(),
            },
            "cancellation_details": {"party": "yoo_money", "reason": "issuer_unavailable"},
        },
    }
    # Ensure webhook uses the same fake redis instance as loader
    import bot.handlers.yookassa_webhook as wh
    from bot.core.loader import redis_client as _fake_redis
    monkeypatch.setattr(wh, "redis_client", _fake_redis, raising=True)

    req = make_webhook_request(payload)
    view = make_yk_view(req)
    _ = await view.post()

    # Subscription becomes past_due, premium off, auto_renew stays True, retry scheduled
    async with sessionmaker() as session:
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id))).scalar_one_or_none()
        assert sub is not None
        assert sub.status == "past_due"
        assert sub.auto_renew is True
        u = (await session.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one_or_none()
        assert u is not None and not bool(u.is_premium)


@pytest.mark.asyncio
async def test_rebill_canceled_permanent_reason_disables_auto_renew(test_db_env, ensure_user, make_webhook_request, make_yk_view, patch_redis_client):
    user_id = await ensure_user(10022)
    # Seed subscription
    async with sessionmaker() as session:
        await session.execute(UserModel.__table__.update().where(UserModel.id == user_id).values(is_premium=True))
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="year",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc),
            expires_at_utc=datetime.now(timezone.utc),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    payload = {
        "event": "payment.canceled",
        "object": {
            "id": "pay_perm_1",
            "metadata": {
                "user_id": user_id,
                "rebill": True,
                "subscription_id": sub_id,
                "period_key": datetime.now(timezone.utc).date().isoformat(),
            },
            "cancellation_details": {"party": "yoo_money", "reason": "permission_revoked"},
        },
    }
    req = make_webhook_request(payload)
    view = make_yk_view(req)
    _ = await view.post()

    async with sessionmaker() as session:
        sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id))).scalar_one_or_none()
        assert sub is not None
        assert sub.status == "past_due"
        assert sub.auto_renew is False
        assert sub.payment_method_id is None
        u = (await session.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one_or_none()
        assert u is not None and not bool(u.is_premium)


@pytest.mark.asyncio
async def test_rebill_canceled_permanent_restricted_and_expired(test_db_env, ensure_user, make_webhook_request, make_yk_view, patch_redis_client):
    # Covers payment_method_restricted and expired_on_confirmation
    for reason in ("payment_method_restricted", "expired_on_confirmation"):
        user_id = await ensure_user(11000 if reason == "payment_method_restricted" else 11001)
        async with sessionmaker() as session:
            sub = SubscriptionModel(
                user_id=user_id,
                status="active",
                plan="month",
                payment_method_id="pm_saved",
                auto_renew=True,
                started_at_utc=datetime.now(timezone.utc),
                expires_at_utc=datetime.now(timezone.utc),
            )
            session.add(sub)
            await session.commit()
            await session.refresh(sub)
            sub_id = sub.id

        payload = {
            "event": "payment.canceled",
            "object": {
                "id": f"pay_perm_{reason}",
                "metadata": {
                    "user_id": user_id,
                    "rebill": True,
                    "subscription_id": sub_id,
                    "period_key": datetime.now(timezone.utc).date().isoformat(),
                },
                "cancellation_details": {"party": "yoo_money", "reason": reason},
            },
        }
        req = make_webhook_request(payload)
        view = make_yk_view(req)
        _ = await view.post()

        async with sessionmaker() as session:
            sub = (await session.execute(select(SubscriptionModel).where(SubscriptionModel.id == sub_id))).scalar_one_or_none()
            assert sub is not None
            assert sub.status == "past_due"
            assert sub.auto_renew is False
            assert sub.payment_method_id is None


@pytest.mark.asyncio
async def test_rebill_canceled_idempotent_duplicate_no_extra_retry(test_db_env, ensure_user, make_webhook_request, make_yk_view, monkeypatch):
    user_id = await ensure_user(10023)
    # Seed subscription
    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc),
            expires_at_utc=datetime.now(timezone.utc),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    period_key = datetime.now(timezone.utc).date().isoformat()
    payload = {
        "event": "payment.canceled",
        "object": {
            "id": "pay_tmp_dup",
            "metadata": {
                "user_id": user_id,
                "rebill": True,
                "subscription_id": sub_id,
                "period_key": period_key,
            },
            "cancellation_details": {"party": "yoo_money", "reason": "issuer_unavailable"},
        },
    }
    # Ensure webhook uses the same fake redis instance as loader
    import bot.handlers.yookassa_webhook as wh
    from bot.core.loader import redis_client as _fake_redis
    monkeypatch.setattr(wh, "redis_client", _fake_redis, raising=True)
    req = make_webhook_request(payload)
    view = make_yk_view(req)
    _ = await view.post()
    # Duplicate
    req2 = make_webhook_request(payload)
    view2 = make_yk_view(req2)
    _ = await view2.post()

    # Assert only one due entry scheduled (idempotent)
    from bot.core.loader import redis_client
    due = await redis_client.zrangebyscore("rebill:due", min="-inf", max=9999999999)
    members = [str(m) for m in due if str(m).startswith(f"{sub_id}:{period_key}")]
    assert len(members) == 1


@pytest.mark.asyncio
async def test_rebill_canceled_clears_submitted_key(test_db_env, ensure_user, make_webhook_request, make_yk_view, monkeypatch):
    user_id = await ensure_user(10024)
    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="month",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc),
            expires_at_utc=datetime.now(timezone.utc),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    period_key = datetime.now(timezone.utc).date().isoformat()
    from bot.core.loader import redis_client
    submitted_key = f"rebill:submitted:{sub_id}:{period_key}"
    await redis_client.set(submitted_key, "1")

    payload = {
        "event": "payment.canceled",
        "object": {
            "id": "pay_tmp_clear_submitted",
            "metadata": {
                "user_id": user_id,
                "rebill": True,
                "subscription_id": sub_id,
                "period_key": period_key,
            },
            "cancellation_details": {"party": "yoo_money", "reason": "issuer_unavailable"},
        },
    }
    req = make_webhook_request(payload)
    view = make_yk_view(req)
    _ = await view.post()

    import bot.handlers.yookassa_webhook as wh
    assert await wh.redis_client.exists(submitted_key) == 0
