from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from bot.core.config import settings
from bot.database.database import sessionmaker
from bot.database.models import PaymentModel, SubscriptionModel, UserModel
from bot.services.yookassa import create_payment, create_recurring_payment


@pytest.mark.asyncio
async def test_create_payment_includes_receipt_payload(test_db_env, ensure_user, monkeypatch) -> None:
    user_id = await ensure_user(10081, email="buyer@example.com")

    # Spy Payment.create to capture payload
    import bot.services.yookassa as yk

    captured = {}

    class _Conf:
        confirmation_url = "https://example/confirm"

    class _Obj:
        id = "pay_create_1"
        confirmation = _Conf()

    def _spy_payment_create(payload, idempotency_key=None):
        captured["payload"] = payload
        captured["idem"] = idempotency_key
        return _Obj()

    monkeypatch.setattr(yk.Payment, "create", _spy_payment_create, raising=True)

    cp = await create_payment(user_id=user_id, plan="month")
    assert cp.payment_id == "pay_create_1"
    assert "payload" in captured

    payload = captured["payload"]
    assert payload["receipt"]["customer"]["email"] == "buyer@example.com"
    items = payload["receipt"]["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    item = items[0]
    assert item["description"] == "Подписка Calorissimo — 30 дней"
    assert item["quantity"] == 1.0
    assert item["amount"]["currency"] == "RUB"
    # Value must equal amount for plan
    expected = f"{settings.PRICE_MONTH_RUB:.2f}"
    assert item["amount"]["value"] == expected
    assert item["vat_code"] == 6
    assert item["payment_mode"] == "full_prepayment"
    assert item["payment_subject"] == "service"


@pytest.mark.asyncio
async def test_create_recurring_payment_includes_receipt_payload(test_db_env, ensure_user, monkeypatch) -> None:
    user_id = await ensure_user(10082, email="rebuyer@example.com")

    # Seed subscription (so FK/linking is valid if enforced)
    async with sessionmaker() as session:
        sub = SubscriptionModel(
            user_id=user_id,
            status="active",
            plan="year",
            payment_method_id="pm_saved",
            auto_renew=True,
            started_at_utc=datetime.now(timezone.utc) - timedelta(days=365),
            expires_at_utc=datetime.now(timezone.utc) - timedelta(days=1),
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        sub_id = sub.id

    import bot.services.yookassa as yk
    captured = {}

    class _Obj:
        id = "pay_rebill_1"

    def _spy_payment_create(payload, idempotency_key=None):
        captured["payload"] = payload
        captured["idem"] = idempotency_key
        return _Obj()

    monkeypatch.setattr(yk.Payment, "create", _spy_payment_create, raising=True)

    cp = await create_recurring_payment(
        user_id=user_id,
        subscription_id=sub_id,
        plan="year",
        payment_method_id="pm_saved",
        period_key=datetime.now(timezone.utc).date().isoformat(),
    )
    assert cp.payment_id == "pay_rebill_1"

    payload = captured["payload"]
    assert payload["receipt"]["customer"]["email"] == "rebuyer@example.com"
    items = payload["receipt"]["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    item = items[0]
    assert item["description"] == "Подписка Calorissimo — 365 дней"
    assert item["quantity"] == 1.0
    assert item["amount"]["currency"] == "RUB"
    expected = f"{settings.PRICE_YEAR_RUB:.2f}"
    assert item["amount"]["value"] == expected
    assert item["vat_code"] == 6
    assert item["payment_mode"] == "full_prepayment"
    assert item["payment_subject"] == "service"


@pytest.mark.asyncio
async def test_webhook_succeeded_saves_receipt_registration(
    test_db_env, ensure_user, make_webhook_request, make_yk_view, yk_stub
) -> None:
    user_id = await ensure_user(10083)

    # Prepare stubbed YooKassa find_one returning receipt_registration
    metadata = {"user_id": user_id, "plan": "month"}
    value = f"{settings.PRICE_MONTH_RUB:.2f}"
    yk_stub(status="succeeded", value=value, metadata=metadata, pm_id="pm_x", pm_saved=True, rr="registered")

    # Trigger webhook
    req = make_webhook_request({
        "event": "payment.succeeded",
        "object": {"id": "yk_payment_receipt_reg"}
    })
    view = make_yk_view(req)
    await view.post()

    # Assert PaymentModel.meta contains receipt_registration
    async with sessionmaker() as session:
        pm = (await session.execute(
            select(PaymentModel).where(PaymentModel.yk_payment_id == "yk_payment_receipt_reg")
        )).scalar_one_or_none()
        assert pm is not None
        meta = dict(pm.meta or {})
        assert meta.get("receipt_registration") == "registered"
        # Also ensure user email exists (not used here but for completeness)
        u = (await session.execute(select(UserModel).where(UserModel.id == user_id))).scalar_one_or_none()
        assert u is not None
