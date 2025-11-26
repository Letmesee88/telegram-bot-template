import asyncio
import types
import pytest

pytestmark = pytest.mark.asyncio


class FakeChat:
    def __init__(self, id=1, type="private"):
        self.id = id
        self.type = type


class FakeFromUser:
    def __init__(self, id=123, language_code="ru"):
        self.id = id
        self.language_code = language_code


class FakeMessage:
    def __init__(self, user_id=123):
        self.from_user = FakeFromUser(user_id)
        self.chat = FakeChat(777, "private")
        # Non-command text to simulate FoodAI text attempt
        self.text = "Привет"
        self._answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self._answers.append((text, kwargs))


class FakeCallbackQuery:
    def __init__(self, data: str, user_id=123):
        self.data = data
        self.from_user = FakeFromUser(user_id)
        self.message = FakeMessage(user_id)
        self._answered = False

    async def answer(self, *args, **kwargs):
        self._answered = True


class FakeSession:
    def __init__(self, exists: bool):
        self._exists = exists

    async def scalar(self, *args, **kwargs):
        # Return primary gating condition: onboarding exists or not
        return 1 if self._exists else None

    async def execute(self, *args, **kwargs):
        class FakeResult:
            def scalars(self):
                class V:
                    def all(self_inner):
                        return []
                return V()
        return FakeResult()

    async def get(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSessionmaker:
    def __init__(self, exists: bool):
        self._exists = exists

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return FakeSession(self._exists)

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
async def _silence_analytics(monkeypatch):
    # Disable analytics sending
    from bot.services.analytics import analytics
    monkeypatch.setattr(analytics, "logger", None, raising=False)
    # Force DB URL to in-memory sqlite to avoid network if anything leaks through
    import os
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    os.environ["DATABASE_URL_ASYNC"] = "sqlite+aiosqlite:///:memory:"
    # Patch i18n '_' in modules under test (no I18n context in unit tests)
    import bot.filters.onboarding_completed as fmod
    monkeypatch.setattr(fmod, "_", lambda s, **kw: s, raising=True)
    import bot.handlers.menu as h_menu
    import bot.handlers.history as h_hist
    import bot.handlers.account as h_acc
    monkeypatch.setattr(h_menu, "_", lambda s, **kw: s, raising=True)
    monkeypatch.setattr(h_hist, "_", lambda s, **kw: s, raising=True)
    monkeypatch.setattr(h_acc, "_", lambda s, **kw: s, raising=True)
    yield


async def _patch_sessionmaker(monkeypatch, exists: bool):
    import bot.database.database as db
    monkeypatch.setattr(db, "sessionmaker", FakeSessionmaker(exists), raising=True)

async def _patch_handlers_sessionmaker(monkeypatch, exists: bool):
    import bot.handlers.menu as menu
    import bot.handlers.history as history
    import bot.handlers.account as account
    monkeypatch.setattr(menu, "sessionmaker", FakeSessionmaker(exists), raising=True)
    monkeypatch.setattr(history, "sessionmaker", FakeSessionmaker(exists), raising=True)
    monkeypatch.setattr(account, "sessionmaker", FakeSessionmaker(exists), raising=True)
    # Patch tz helper to avoid DB usage inside handlers if they get past the gate
    import bot.services.users as users
    from datetime import timezone as _tz
    async def _tz_stub(session, user_id):
        return _tz.utc
    monkeypatch.setattr(users, "get_user_tzinfo", _tz_stub, raising=True)


# --- OnboardingCompletedFilter tests ---
async def test_onboarding_filter_blocks_message_without_onboarding(monkeypatch):
    from bot.filters.onboarding_completed import OnboardingCompletedFilter

    msg = FakeMessage(user_id=42)
    await _patch_sessionmaker(monkeypatch, exists=False)

    # Filter signature requires an AsyncSession via DI; we pass FakeSession directly
    filt = OnboardingCompletedFilter()
    ok = await filt(msg, FakeSession(False))

    assert ok is False
    assert any("онбординг" in t.lower() for t, _ in msg._answers)


async def test_onboarding_filter_allows_when_onboarding_exists(monkeypatch):
    from bot.filters.onboarding_completed import OnboardingCompletedFilter

    msg = FakeMessage(user_id=42)
    await _patch_sessionmaker(monkeypatch, exists=True)

    filt = OnboardingCompletedFilter()
    ok = await filt(msg, FakeSession(True))

    assert ok is True
    assert len(msg._answers) == 0


async def test_onboarding_filter_blocks_callback_without_onboarding(monkeypatch):
    from bot.filters.onboarding_completed import OnboardingCompletedFilter

    cb = FakeCallbackQuery(data="any:cb", user_id=42)
    await _patch_sessionmaker(monkeypatch, exists=False)

    filt = OnboardingCompletedFilter()
    ok = await filt(cb, FakeSession(False))

    assert ok is False
    assert cb._answered is True
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


# --- /day gating ---
async def test_cmd_day_gated_without_onboarding(monkeypatch):
    from bot.handlers.menu import cmd_day

    await _patch_sessionmaker(monkeypatch, exists=False)
    await _patch_handlers_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=11)

    await cmd_day(m)
    # Expect CTA reply
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_cb_diary_today_gated_without_onboarding(monkeypatch):
    from bot.handlers.menu import cb_diary_today

    await _patch_sessionmaker(monkeypatch, exists=False)
    await _patch_handlers_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="diary:today:1", user_id=11)

    await cb_diary_today(cb)
    assert cb._answered is True
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


# --- /history gating ---
async def test_cmd_history_gated_without_onboarding(monkeypatch):
    from bot.handlers.history import cmd_history

    await _patch_sessionmaker(monkeypatch, exists=False)
    await _patch_handlers_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=12)

    await cmd_history(m)
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_cb_history_back_gated_without_onboarding(monkeypatch):
    from bot.handlers.history import cb_history_back

    await _patch_sessionmaker(monkeypatch, exists=False)
    await _patch_handlers_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="history:back", user_id=12)

    await cb_history_back(cb)
    assert cb._answered is True
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


async def test_cb_history_day_gated_without_onboarding(monkeypatch):
    from bot.handlers.history import cb_history_day

    await _patch_sessionmaker(monkeypatch, exists=False)
    await _patch_handlers_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="history:day:2025-01-01", user_id=12)

    await cb_history_day(cb)
    assert cb._answered is True
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


# --- /account gating ---
async def test_cmd_account_gated_without_onboarding(monkeypatch):
    from bot.handlers.account import cmd_account

    await _patch_sessionmaker(monkeypatch, exists=False)
    await _patch_handlers_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=13)

    await cmd_account(m)
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_cb_account_open_gated_without_onboarding(monkeypatch):
    from bot.handlers.account import cb_account_open

    await _patch_sessionmaker(monkeypatch, exists=False)
    await _patch_handlers_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="account:open:today", user_id=13)

    await cb_account_open(cb)
    assert cb._answered is True
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


# --- FoodAIEnabledFilter tests ---
class _FakeUserDB:
    def __init__(self, is_premium: bool, foodai_enabled: bool):
        self.is_premium = is_premium
        self.foodai_enabled_at = object() if foodai_enabled else None


class _FakeSessionFoodAI(FakeSession):
    def __init__(self, is_premium: bool, foodai_enabled: bool):
        super().__init__(exists=True)
        self._user = _FakeUserDB(is_premium, foodai_enabled)

    async def get(self, *args, **kwargs):
        return self._user


async def test_foodai_filter_allows_when_premium_and_enabled():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    msg = FakeMessage(user_id=55)
    session = _FakeSessionFoodAI(is_premium=True, foodai_enabled=True)

    ok = await FoodAIEnabledFilter()(msg, session)
    assert ok is True
    assert len(msg._answers) == 0


async def test_foodai_filter_blocks_message_with_cta_on_message():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    msg = FakeMessage(user_id=56)
    session = _FakeSessionFoodAI(is_premium=False, foodai_enabled=True)

    ok = await FoodAIEnabledFilter()(msg, session)
    assert ok is False
    # CTA text should be sent
    assert any("подписка не активна" in t.lower() for t, _ in msg._answers)
    # And include the correct CTA button
    rmks = [kw.get("reply_markup") for _, kw in msg._answers if isinstance(kw, dict)]
    kb = next((r for r in rmks if r is not None), None)
    assert kb is not None
    btn = kb.inline_keyboard[0][0]
    assert getattr(btn, "text", "") == "💎 Выбрать тариф"
    assert getattr(btn, "callback_data", "") == "sale:choose"


async def test_foodai_filter_blocks_callback_with_cta_and_ack():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    cb = FakeCallbackQuery(data="foodai:any", user_id=57)
    session = _FakeSessionFoodAI(is_premium=True, foodai_enabled=False)

    ok = await FoodAIEnabledFilter()(cb, session)
    assert ok is False
    assert cb._answered is True
    assert any("подписка не активна" in t.lower() for t, _ in cb.message._answers)
    rmks = [kw.get("reply_markup") for _, kw in cb.message._answers if isinstance(kw, dict)]
    kb = next((r for r in rmks if r is not None), None)
    assert kb is not None
    btn = kb.inline_keyboard[0][0]
    assert getattr(btn, "text", "") == "💎 Выбрать тариф"
    assert getattr(btn, "callback_data", "") == "sale:choose"


# --- YooKassa webhook: revoke FoodAI on rebill cancellation ---
class _UpdateStub:
    def __init__(self, target):
        self.target = target
        self._values = None

    def where(self, *args, **kwargs):
        return self

    def values(self, **kwargs):
        self._values = kwargs
        return self


class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _FakeSessionRec:
    def __init__(self):
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult()

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSessionmakerRec:
    def __init__(self, session):
        self._session = session

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeReq:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeBot:
    async def send_message(self, *args, **kwargs):
        return None


class _FakeRedis:
    def __init__(self):
        self._store = {}

    async def set(self, *args, **kwargs):
        return True

    async def delete(self, *args, **kwargs):
        return 1

    async def incr(self, key):
        self._store[key] = int(self._store.get(key, 0)) + 1
        return self._store[key]

    async def expire(self, *args, **kwargs):
        return True

    async def zadd(self, *args, **kwargs):
        return True


async def test_yk_webhook_canceled_rebill_revokes_foodai(monkeypatch):
    import bot.handlers.yookassa_webhook as yk
    from bot.database.models import UserModel as _UserModel

    # Patch builder 'update' in this module to our stub
    def _upd(model):
        return _UpdateStub(model)
    monkeypatch.setattr(yk, "update", _upd, raising=True)

    # Patch sessionmaker to a recorder
    rec_session = _FakeSessionRec()
    monkeypatch.setattr(yk, "sessionmaker", _FakeSessionmakerRec(rec_session), raising=True)

    # Patch external deps used later in flow
    monkeypatch.setattr(yk, "bot", _FakeBot(), raising=True)
    # Minimal redis mock
    monkeypatch.setattr(yk, "redis_client", _FakeRedis(), raising=True)

    # Prepare webhook view with fake request
    payload = {
        "event": "payment.canceled",
        "object": {
            "id": "pay_1",
            "metadata": {
                "user_id": 999,
                "rebill": True,
                "subscription_id": 123,
                "period_key": "2025-12",
            },
        },
    }
    view = yk.YooKassaWebhookView(_FakeReq(payload))

    resp = await view.post()
    assert getattr(resp, "text", "OK") == "OK"

    # Ensure we attempted to set is_premium=False and foodai_enabled_at=None for UserModel
    found = False
    for stmt in rec_session.executed:
        if isinstance(stmt, _UpdateStub) and stmt.target is _UserModel and stmt._values:
            if stmt._values.get("is_premium") is False and ("foodai_enabled_at" in stmt._values) and (stmt._values["foodai_enabled_at"] is None):
                found = True
                break
    assert found is True
