import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot.handlers.yookassa_webhook import Payment as YKPayment
from bot.handlers.yookassa_webhook import YooKassaWebhookView as View
from bot.handlers.yookassa_webhook import settings as app_settings


class _FakeResult:
    def __init__(self, value=None) -> None:
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _FakeSession:
    def __init__(self, result=None, on_execute=None) -> None:
        self._result = result
        self._on_execute = on_execute
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        if self._on_execute:
            self._on_execute(args, kwargs)
        self.executed.append((args, kwargs))
        return _FakeResult(self._result)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    def add(self, *args, **kwargs) -> None:
        return None


async def _make_client():
    app = web.Application()
    app.router.add_view("/yookassa/webhook", View)

    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()

    async def _request(method: str, path: str, **kw):
        resp = await client.request(method, path, **kw)
        text = await resp.text()
        return resp.status, text

    async def _close() -> None:
        await client.close()
        await server.close()

    return types.SimpleNamespace(request=_request, close=_close)


@pytest.mark.asyncio
async def test_cancel_ignored_when_payment_not_in_db(monkeypatch) -> None:
    # Ensure YooKassa config is present
    app_settings.YOOKASSA_SHOP_ID = "test"
    app_settings.YOOKASSA_SECRET_KEY = "test"

    # Patch YooKassa Payment.find_one to return minimal object with no metadata
    class _Obj:
        metadata = {}

    monkeypatch.setattr(YKPayment, "find_one", lambda pid: _Obj())

    # sessionmaker sequence: first context returns no payment in DB
    from bot.handlers import yookassa_webhook as mod

    seq = [
        _FakeSession(result=None),  # select payment by id -> None
    ]

    def _factory():
        if seq:
            return seq.pop(0)
        return _FakeSession(result=None)

    monkeypatch.setattr(mod, "sessionmaker", _factory)

    c = await _make_client()
    try:
        status, text = await c.request(
            "POST",
            "/yookassa/webhook",
            json={"event": "payment.canceled", "object": {"id": "pay_fake"}},
        )
        assert status == 200
        assert text == "OK"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_cancel_updates_payment_when_verified(monkeypatch) -> None:
    app_settings.YOOKASSA_SHOP_ID = "test"
    app_settings.YOOKASSA_SECRET_KEY = "test"

    # YooKassa returns metadata that marks rebill and links to subscription
    class _YKObj:
        def __init__(self) -> None:
            self.metadata = {"user_id": 100, "rebill": True, "subscription_id": 42, "period_key": "2025-12"}

    monkeypatch.setattr(YKPayment, "find_one", lambda pid: _YKObj())

    # Prepare a fake DB payment row
    class _Pm:
        id = 1
        subscription_id = 42
        user_id = 100
        meta = {}

    updated = {"called": False, "values": None}

    def _on_execute(args, kwargs) -> None:
        # Capture UPDATE calls (very loose check)
        txt = str(args[0])
        if "UPDATE" in txt or "update" in txt.lower():
            updated["called"] = True
            updated["values"] = kwargs.get("params") or {}

    from bot.handlers import yookassa_webhook as mod

    seq = [
        _FakeSession(result=_Pm()),   # first select -> pm exists
        _FakeSession(on_execute=_on_execute),  # update payment -> capture
        _FakeSession(),  # subscription/user updates
        _FakeSession(),  # maybe more contexts
    ]

    def _factory():
        if seq:
            return seq.pop(0)
        return _FakeSession()

    monkeypatch.setattr(mod, "sessionmaker", _factory)

    # Stub bot to avoid real Telegram calls
    class _Bot:
        async def send_message(self, *a, **k) -> None:
            return None

    monkeypatch.setattr(mod, "bot", _Bot())

    # Stub redis client
    class _Redis:
        async def delete(self, *a, **k) -> int:
            return 1
        async def zadd(self, *a, **k) -> int:
            return 1
        async def incr(self, *a, **k) -> int:
            return 1
        async def expire(self, *a, **k) -> int:
            return 1
        async def set(self, *a, **k) -> bool:
            return True

    monkeypatch.setattr(mod, "redis_client", _Redis())

    c = await _make_client()
    try:
        status, text = await c.request(
            "POST",
            "/yookassa/webhook",
            json={"event": "payment.canceled", "object": {"id": "pay_123"}},
        )
        assert status == 200
        assert text == "OK"
        # We expect that update was attempted for PaymentModel with status=canceled
        assert updated["called"] is True
    finally:
        await c.close()
