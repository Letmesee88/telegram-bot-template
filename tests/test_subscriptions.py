from datetime import datetime, timedelta, timezone
from typing import NoReturn

import pytest

import bot.handlers.yookassa_webhook as webhook
import bot.services.yookassa as svc


class FakeResult:
    def __init__(self, scalar_obj=None, scalars_list=None) -> None:
        self._scalar_obj = scalar_obj
        self._scalars_list = scalars_list or []

    def scalar_one_or_none(self):
        return self._scalar_obj

    class _Scalars:
        def __init__(self, items) -> None:
            self._items = items

        def all(self):
            return list(self._items)

    def scalars(self):
        return FakeResult._Scalars(self._scalars_list)


class SelectPromise:
    def __init__(self, model) -> None:
        self.model = model

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self


def fake_select(model):
    return SelectPromise(model)


class UpdatePromise:
    def __init__(self, model) -> None:
        self.model = model
        self._values = {}

    def where(self, *args, **kwargs):
        return self

    def values(self, **values):
        self._values = dict(values)
        return self


def fake_update(model):
    return UpdatePromise(model)


class FakePaymentModel:
    def __init__(self, *, id: int, user_id: int, yk_payment_id: str, status: str = "pending", meta: dict | None = None) -> None:
        self.id = id
        self.user_id = user_id
        self.subscription_id = None
        self.yk_payment_id = yk_payment_id
        self.idempotence_key = None
        self.payment_method_id = None
        self.amount_value = None
        self.currency = "RUB"
        self.status = status
        self.description = None
        self.meta = dict(meta or {})
        self.captured_at_utc = None


class FakeSubscriptionModel:
    def __init__(self, *, id: int, user_id: int, status: str, plan: str, expires_at_utc: datetime,
                 auto_renew: bool = False, payment_method_id: str | None = None) -> None:
        self.id = id
        self.user_id = user_id
        self.status = status
        self.plan = plan
        self.payment_method_id = payment_method_id
        self.started_at_utc = datetime.now(timezone.utc)
        self.expires_at_utc = expires_at_utc
        self.auto_renew = auto_renew


class FakeUserModel:
    def __init__(self, *, id: int) -> None:
        self.id = id
        self.is_premium = False
        self.foodai_enabled_at = None


class FakeSession:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.update_calls = []

    async def execute(self, query):
        # SELECT
        if isinstance(query, SelectPromise):
            if query.model.__name__.lower().endswith("paymentmodel"):
                pm = self.state.get("payment")
                return FakeResult(scalar_obj=pm, scalars_list=self.state.get("payment_rows", []))
            if query.model.__name__.lower().endswith("subscriptionmodel"):
                sub = self.state.get("subscription")
                return FakeResult(scalar_obj=sub)
            if query.model.__name__.lower().endswith("usermodel"):
                # For webhook: selection happens only for updates, we don't SELECT user
                return FakeResult(scalar_obj=None)
            # Default empty
            return FakeResult(scalar_obj=None, scalars_list=[])
        # UPDATE
        if isinstance(query, UpdatePromise):
            self.update_calls.append((query.model, dict(query._values)))
            if query.model.__name__.lower().endswith("paymentmodel"):
                pm = self.state.get("payment")
                if pm:
                    for k, v in query._values.items():
                        setattr(pm, k, v)
                return FakeResult()
            if query.model.__name__.lower().endswith("subscriptionmodel"):
                sub = self.state.get("subscription")
                if sub:
                    for k, v in query._values.items():
                        setattr(sub, k, v)
                return FakeResult()
            if query.model.__name__.lower().endswith("usermodel"):
                user = self.state.get("user")
                if user:
                    for k, v in query._values.items():
                        setattr(user, k, v)
                return FakeResult()
            return FakeResult()
        # Fallback
        return FakeResult()

    async def scalar(self, query):
        # Used in create_payment to fetch UserModel.email; just return a dummy
        return self.state.get("user_email", "user@example.com")

    async def commit(self) -> None:
        return None


class FakeSessionCM:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeBot:
    async def send_message(self, *args, **kwargs) -> None:
        return None


class FakeRedis:
    def __init__(self) -> None:
        self.calls = []
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.zsets: dict[str, dict[str, int]] = {}

    async def delete(self, key) -> None:
        self.calls.append(("delete", (key,), {}))
        self.kv.pop(key, None)
        self.ttl.pop(key, None)

    async def set(self, key, value, nx: bool | None = None, ex: int | None = None) -> bool:
        self.calls.append(("set", (key, value), {"nx": nx, "ex": ex}))
        if nx and key in self.kv:
            return False
        self.kv[key] = str(value)
        if ex:
            self.ttl[key] = int(ex)
        return True

    async def exists(self, key) -> int:
        return 1 if key in self.kv else 0

    async def zadd(self, key, mapping: dict) -> None:
        self.calls.append(("zadd", (key, mapping), {}))
        z = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            z[str(member)] = int(score)

    async def zrangebyscore(self, key, min: str | int, max: int, start=0, num=200, withscores=False):
        z = self.zsets.get(key, {})
        minv = -10**18 if min == "-inf" else int(min)
        maxv = int(max)
        items = [(m, s) for m, s in z.items() if s >= minv and s <= maxv]
        items.sort(key=lambda x: x[1])
        slice_items = items[start:start+num]
        if withscores:
            return slice_items
        return [m for m, _ in slice_items]

    async def zrem(self, key, member) -> None:
        z = self.zsets.get(key, {})
        z.pop(member if isinstance(member, str) else member.decode(), None)

    class _Pipeline:
        def __init__(self, outer: "FakeRedis") -> None:
            self.outer = outer
            self.ops = []

        def zrem(self, key, member) -> None:
            self.ops.append(("zrem", key, member))

        async def execute(self) -> None:
            for op, key, member in self.ops:
                if op == "zrem":
                    await self.outer.zrem(key, member)

    def pipeline(self, transaction=False):
        return FakeRedis._Pipeline(self)

    async def incr(self, key):
        self.calls.append(("incr", (key,), {}))
        try:
            cur = int(self.kv.get(key, "0"))
        except Exception:
            cur = 0
        cur += 1
        self.kv[key] = str(cur)
        return cur

    async def expire(self, key, ttl) -> None:
        self.calls.append(("expire", (key, ttl), {}))
        self.ttl[key] = int(ttl)


class FakeYkAmount:
    def __init__(self, value: str, currency: str = "RUB") -> None:
        self.value = value
        self.currency = currency


class FakeYkPaymentMethod:
    def __init__(self, id: str, saved: bool) -> None:
        self.id = id
        self.saved = saved


class FakeYkPayment:
    def __init__(self, *, status: str, amount_value: str, currency: str, metadata: dict,
                 payment_method_saved: bool = False, payment_method_id: str | None = None, receipt_registration=None) -> None:
        self.status = status
        self.amount = FakeYkAmount(amount_value, currency)
        self.metadata = metadata
        self.payment_method = FakeYkPaymentMethod(payment_method_id, payment_method_saved) if payment_method_id else None
        self.receipt_registration = receipt_registration


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_block_repeat_trial_in_create_payment(monkeypatch) -> None:
    # Patch credentials so _configure passes
    monkeypatch.setattr(svc.settings, "YOOKASSA_SHOP_ID", "test")
    monkeypatch.setattr(svc.settings, "YOOKASSA_SECRET_KEY", "test")

    # Fake session: returns succeeded trial row
    class P:
        def __init__(self, meta) -> None:
            self.meta = meta
            self.status = "succeeded"

    state = {"payment_rows": [P({"plan": "trial"})]}
    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))

    monkeypatch.setattr(svc, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(svc, "select", fake_select)

    with pytest.raises(RuntimeError) as ei:
        await svc.create_payment(user_id=111, plan="trial")
    assert str(ei.value) == "trial_already_used"


@pytest.mark.asyncio
async def test_no_downgrade_trial_on_active_month(monkeypatch) -> None:
    # Patch settings/prices and config
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SHOP_ID", "x")
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SECRET_KEY", "y")
    monkeypatch.setattr(webhook.settings, "PRICE_TRIAL_RUB", 10)

    # Patch builders and sessionmaker
    state = {}
    pm = FakePaymentModel(id=1, user_id=123, yk_payment_id="pay_1", status="pending", meta={})
    sub = FakeSubscriptionModel(
        id=5,
        user_id=123,
        status="active",
        plan="month",
        expires_at_utc=datetime.now(timezone.utc) + timedelta(days=30),
        auto_renew=False,
        payment_method_id=None,
    )
    state["payment"] = pm
    state["subscription"] = sub
    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))

    monkeypatch.setattr(webhook, "select", fake_select)
    monkeypatch.setattr(webhook, "update", fake_update)
    monkeypatch.setattr(webhook, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(webhook, "bot", FakeBot())
    monkeypatch.setattr(webhook, "redis_client", FakeRedis())

    async def fake_tzinfo(session, user_id):
        return timezone.utc
    monkeypatch.setattr(webhook, "get_user_tzinfo", fake_tzinfo)

    # Fake YooKassa payment
    yk = FakeYkPayment(
        status="succeeded",
        amount_value="10.00",
        currency="RUB",
        metadata={"plan": "trial", "user_id": 123, "rebill": False},
        payment_method_saved=True,
        payment_method_id="pm_42",
    )
    monkeypatch.setattr(webhook.Payment, "find_one", lambda pid: yk)

    view = webhook.YooKassaWebhookView(FakeRequest({
        "event": "payment.succeeded",
        "object": {"id": "pay_1"},
    }))
    resp = await view.post()
    assert hasattr(resp, "text")

    # Assertions: plan and expiry unchanged; payment linked; auto_renew possibly enabled; PM id filled
    assert sub.plan == "month"
    assert sub.expires_at_utc > datetime.now(timezone.utc)
    assert pm.subscription_id == sub.id or sub.payment_method_id == "pm_42"
    # Auto-renew should be enabled due to saved method
    assert getattr(sub, "auto_renew", False) is True
    assert sub.payment_method_id == "pm_42"


@pytest.mark.asyncio
async def test_autorenew_enabled_on_non_rebill_success(monkeypatch) -> None:
    # Patch settings/prices and config
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SHOP_ID", "x")
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SECRET_KEY", "y")
    monkeypatch.setattr(webhook.settings, "PRICE_MONTH_RUB", 750)

    state = {}
    pm = FakePaymentModel(id=1, user_id=123, yk_payment_id="pay_2", status="pending", meta={})
    sub = FakeSubscriptionModel(
        id=6,
        user_id=123,
        status="active",
        plan="month",
        expires_at_utc=datetime.now(timezone.utc) - timedelta(days=1),
        auto_renew=False,
        payment_method_id=None,
    )
    state["payment"] = pm
    state["subscription"] = sub

    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))

    monkeypatch.setattr(webhook, "select", fake_select)
    monkeypatch.setattr(webhook, "update", fake_update)
    monkeypatch.setattr(webhook, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(webhook, "bot", FakeBot())
    monkeypatch.setattr(webhook, "redis_client", FakeRedis())

    async def fake_tzinfo(session, user_id):
        return timezone.utc
    monkeypatch.setattr(webhook, "get_user_tzinfo", fake_tzinfo)

    yk = FakeYkPayment(
        status="succeeded",
        amount_value="750.00",
        currency="RUB",
        metadata={"plan": "month", "user_id": 123, "rebill": False},
        payment_method_saved=True,
        payment_method_id="pm_77",
    )
    monkeypatch.setattr(webhook.Payment, "find_one", lambda pid: yk)

    view = webhook.YooKassaWebhookView(FakeRequest({
        "event": "payment.succeeded",
        "object": {"id": "pay_2"},
    }))
    await view.post()

    assert getattr(sub, "auto_renew", False) is True
    assert sub.payment_method_id == "pm_77"


@pytest.mark.asyncio
async def test_canceled_expired_on_confirmation_keeps_autorenew(monkeypatch) -> None:
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SHOP_ID", "x")
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SECRET_KEY", "y")

    state = {}
    pm = FakePaymentModel(id=10, user_id=123, yk_payment_id="pay_3", status="pending", meta={})
    pm.subscription_id = 7
    sub = FakeSubscriptionModel(
        id=7,
        user_id=123,
        status="active",
        plan="year",
        expires_at_utc=datetime.now(timezone.utc) + timedelta(days=200),
        auto_renew=True,
        payment_method_id="pm_99",
    )
    state["payment"] = pm
    state["subscription"] = sub
    state["user"] = FakeUserModel(id=123)

    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))

    monkeypatch.setattr(webhook, "select", fake_select)
    monkeypatch.setattr(webhook, "update", fake_update)
    monkeypatch.setattr(webhook, "sessionmaker", fake_sessionmaker)
    fake_redis = FakeRedis()
    monkeypatch.setattr(webhook, "redis_client", fake_redis)
    monkeypatch.setattr(webhook, "bot", FakeBot())

    # Fake YK payment (for canceled path, webhook also calls find_one)
    yk = FakeYkPayment(
        status="canceled",
        amount_value="2500.00",
        currency="RUB",
        metadata={"user_id": 123, "plan": "year", "rebill": True, "subscription_id": 7, "period_key": "2025-12"},
        payment_method_saved=True,
        payment_method_id="pm_99",
    )
    monkeypatch.setattr(webhook.Payment, "find_one", lambda pid: yk)

    view = webhook.YooKassaWebhookView(FakeRequest({
        "event": "payment.canceled",
        "object": {
            "id": "pay_3",
            "cancellation_details": {"party": "yoo_kassa", "reason": "expired_on_confirmation"},
        },
    }))
    await view.post()

    # Should not disable auto_renew or clear payment_method_id
    assert sub.auto_renew is True
    assert sub.payment_method_id == "pm_99"
    # Status should be set to past_due
    assert sub.status == "past_due"


@pytest.mark.asyncio
async def test_canceled_permission_revoked_disables_autorenew(monkeypatch) -> None:
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SHOP_ID", "x")
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SECRET_KEY", "y")

    state = {}
    pm = FakePaymentModel(id=11, user_id=123, yk_payment_id="pay_4", status="pending", meta={})
    pm.subscription_id = 8
    sub = FakeSubscriptionModel(
        id=8,
        user_id=123,
        status="active",
        plan="year",
        expires_at_utc=datetime.now(timezone.utc) + timedelta(days=200),
        auto_renew=True,
        payment_method_id="pm_100",
    )
    state["payment"] = pm
    state["subscription"] = sub
    state["user"] = FakeUserModel(id=123)

    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))

    monkeypatch.setattr(webhook, "select", fake_select)
    monkeypatch.setattr(webhook, "update", fake_update)
    monkeypatch.setattr(webhook, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(webhook, "redis_client", FakeRedis())
    monkeypatch.setattr(webhook, "bot", FakeBot())

    yk = FakeYkPayment(
        status="canceled",
        amount_value="2500.00",
        currency="RUB",
        metadata={"user_id": 123, "plan": "year", "rebill": True, "subscription_id": 8, "period_key": "2025-12"},
        payment_method_saved=True,
        payment_method_id="pm_100",
    )
    monkeypatch.setattr(webhook.Payment, "find_one", lambda pid: yk)

    view = webhook.YooKassaWebhookView(FakeRequest({
        "event": "payment.canceled",
        "object": {
            "id": "pay_4",
            "cancellation_details": {"party": "yoo_kassa", "reason": "permission_revoked"},
        },
    }))
    await view.post()

    assert sub.auto_renew is False
    assert sub.payment_method_id is None


@pytest.mark.asyncio
async def test_webhook_rebill_canceled_schedules_retry_single(monkeypatch) -> None:
    # Setup
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SHOP_ID", "x")
    monkeypatch.setattr(webhook.settings, "YOOKASSA_SECRET_KEY", "y")
    state = {}
    pm = FakePaymentModel(id=21, user_id=500, yk_payment_id="pay_retry_1", status="pending", meta={})
    pm.subscription_id = 501
    sub = FakeSubscriptionModel(
        id=501,
        user_id=500,
        status="active",
        plan="year",
        expires_at_utc=datetime.now(timezone.utc) + timedelta(days=1),
        auto_renew=True,
        payment_method_id="pm_X",
    )
    state["payment"] = pm
    state["subscription"] = sub
    state["user"] = FakeUserModel(id=500)

    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))
    fr = FakeRedis()
    monkeypatch.setattr(webhook, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(webhook, "select", fake_select)
    monkeypatch.setattr(webhook, "update", fake_update)
    monkeypatch.setattr(webhook, "redis_client", fr)
    monkeypatch.setattr(webhook, "bot", FakeBot())

    # YooKassa payment metadata signals rebill cancel (not permanent)
    yk = FakeYkPayment(
        status="canceled",
        amount_value="2500.00",
        currency="RUB",
        metadata={"user_id": 500, "plan": "year", "rebill": True, "subscription_id": 501, "period_key": "2025-12"},
        payment_method_saved=True,
        payment_method_id="pm_X",
    )
    monkeypatch.setattr(webhook.Payment, "find_one", lambda pid: yk)

    payload = {
        "event": "payment.canceled",
        "object": {
            "id": "pay_retry_1",
            "cancellation_details": {"party": "yoo_kassa", "reason": "expired_on_confirmation"},
        },
    }
    view = webhook.YooKassaWebhookView(FakeRequest(payload))
    await view.post()
    # First cancel should schedule retry and set attempts=1 and processed flag
    assert any(call[0] == "incr" for call in fr.calls)
    assert "rebill:attempts:501:2025-12" in fr.kv
    # ZSET has due member
    assert "rebill:due" in fr.zsets
    assert any(m.startswith("501:2025-12") for m in fr.zsets["rebill:due"])
    assert "rebill:canceled:processed:501:2025-12" in fr.kv

    # Second identical webhook should be de-duplicated (processed flag blocks double scheduling)
    fr.calls.clear()
    await view.post()
    # No second incr
    assert not any(call[0] == "incr" for call in fr.calls)


@pytest.mark.asyncio
async def test_scheduler_due_retry_flow_failure(monkeypatch) -> None:
    # Force scheduler to see one due retry and fail create_recurring_payment, then plan next retry
    import bot.background.recurring_scheduler as sched

    # Fake DB state
    sub = FakeSubscriptionModel(
        id=601,
        user_id=600,
        status="active",
        plan="year",
        expires_at_utc=datetime.now(timezone.utc) - timedelta(seconds=1),
        auto_renew=True,
        payment_method_id="pm_Y",
    )
    state = {"subscription": sub}

    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))
    monkeypatch.setattr(sched, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(sched, "select", fake_select)
    monkeypatch.setattr(sched, "update", fake_update)

    # Fake Redis with one due entry
    fr = FakeRedis()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    await fr.zadd(sched.ZSET_DUE, {"601:2025-12": now_ts})
    monkeypatch.setattr(sched, "redis_client", fr)

    # Make create_recurring_payment fail to trigger attempts++ and reschedule
    async def fail_recurring(**kwargs) -> NoReturn:
        msg = "yk down"
        raise RuntimeError(msg)
    monkeypatch.setattr(sched.yk, "create_recurring_payment", fail_recurring)

    # Fake bot
    class _Bot:
        async def send_message(self, *a, **kw) -> None:
            return None
    bot = _Bot()

    rs = sched.RecurringScheduler()
    rs._bot = bot  # inject fake bot for notifications
    # Directly call the internal step that processes due retries
    await rs._process_due_retries()
    # Validate: attempts incremented and next retry scheduled
    assert fr.kv.get(sched.ATTEMPTS_FMT.format(sub_id=601, period="2025-12")) == "1"
    assert sched.ZSET_DUE in fr.zsets
    assert any(m.startswith("601:2025-12") for m in fr.zsets[sched.ZSET_DUE])


@pytest.mark.asyncio
async def test_scheduler_due_retry_flow_success(monkeypatch) -> None:
    # On success: set submitted flag, do not increment attempts, do not schedule new zadd
    import bot.background.recurring_scheduler as sched

    # Fake DB state
    sub = FakeSubscriptionModel(
        id=701,
        user_id=700,
        status="active",
        plan="year",
        expires_at_utc=datetime.now(timezone.utc) - timedelta(seconds=1),
        auto_renew=True,
        payment_method_id="pm_Z",
    )
    state = {"subscription": sub}

    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))
    monkeypatch.setattr(sched, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(sched, "select", fake_select)
    monkeypatch.setattr(sched, "update", fake_update)

    fr = FakeRedis()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    member = "701:2025-12"
    await fr.zadd(sched.ZSET_DUE, {member: now_ts})
    monkeypatch.setattr(sched, "redis_client", fr)

    class CP:
        def __init__(self) -> None:
            self.payment_id = "pay_ok"
            self.confirmation_url = ""
            self.idempotence_key = "rebill:701:2025-12"

    async def ok_recurring(**kwargs):
        return CP()
    monkeypatch.setattr(sched.yk, "create_recurring_payment", ok_recurring)

    class _Bot:
        async def send_message(self, *a, **kw) -> None:
            return None
    bot = _Bot()

    rs = sched.RecurringScheduler()
    rs._bot = bot
    await rs._process_due_retries()

    submitted_key = sched.SUBMITTED_FMT.format(sub_id=701, period="2025-12")
    assert fr.kv.get(submitted_key) == "rebill:701:2025-12"
    # No attempts increment
    assert sched.ATTEMPTS_FMT.format(sub_id=701, period="2025-12") not in fr.kv
    # No new due scheduled; and original due removed
    assert member not in fr.zsets.get(sched.ZSET_DUE, {})
