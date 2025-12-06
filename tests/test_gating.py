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
            def first(self):
                return None
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
    import bot.handlers.gate as h_gate
    import bot.handlers.templates as h_tpl
    import bot.handlers.foodai as h_foodai
    monkeypatch.setattr(h_menu, "_", lambda s, **kw: s, raising=True)
    monkeypatch.setattr(h_hist, "_", lambda s, **kw: s, raising=True)
    monkeypatch.setattr(h_acc, "_", lambda s, **kw: s, raising=True)
    monkeypatch.setattr(h_gate, "_", lambda s, **kw: s, raising=True)
    monkeypatch.setattr(h_tpl, "_", lambda s, **kw: s.format(**kw) if "{" in s else s, raising=True)
    monkeypatch.setattr(h_foodai, "_", lambda s, **kw: s.format(**kw) if "{" in s else s, raising=True)
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


async def test_foodai_filter_allows_when_premium_and_enabled(monkeypatch):
    from bot.filters.foodai_enabled import FoodAIEnabledFilter
    import bot.filters.foodai_enabled as fmod

    async def _active(session, user_id, include_grace=False):
        return True
    monkeypatch.setattr(fmod, "is_subscription_active", _active, raising=True)

    msg = FakeMessage(user_id=55)
    session = _FakeSessionFoodAI(is_premium=True, foodai_enabled=True)

    ok = await FoodAIEnabledFilter()(msg, session)
    assert ok is True
    assert len(msg._answers) == 0


async def test_foodai_filter_blocks_message_with_cta_on_message(monkeypatch):
    from bot.filters.foodai_enabled import FoodAIEnabledFilter
    import bot.filters.foodai_enabled as fmod

    async def _inactive(session, user_id, include_grace=False):
        return False
    monkeypatch.setattr(fmod, "is_subscription_active", _inactive, raising=True)

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


async def test_foodai_filter_does_not_send_cta_on_callback_any():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    cb = FakeCallbackQuery(data="foodai:any", user_id=57)
    session = _FakeSessionFoodAI(is_premium=True, foodai_enabled=False)

    ok = await FoodAIEnabledFilter()(cb, session)
    assert ok is False
    # Filter should not answer nor send CTA for callbacks
    assert cb._answered is False
    assert len(cb.message._answers) == 0


async def test_foodai_filter_allows_premium_even_if_flag_missing_on_text(monkeypatch):
    from bot.filters.foodai_enabled import FoodAIEnabledFilter
    import bot.filters.foodai_enabled as fmod

    async def _active(session, user_id, include_grace=False):
        return True
    monkeypatch.setattr(fmod, "is_subscription_active", _active, raising=True)

    msg = FakeMessage(user_id=58)
    session = _FakeSessionFoodAI(is_premium=True, foodai_enabled=False)

    ok = await FoodAIEnabledFilter()(msg, session)
    assert ok is True
    assert len(msg._answers) == 0


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


# --- FoodAIEnabledFilter should NOT send CTA during FSM (onboarding) ---
class FakeState:
    def __init__(self, state_value: str | None = "onboarding:any"):
        self._state_value = state_value

    async def get_state(self):
        return self._state_value


async def test_foodai_filter_skips_cta_when_fsm_active_text():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    msg = FakeMessage(user_id=61)
    msg.text = "это текст"
    session = _FakeSessionFoodAI(is_premium=False, foodai_enabled=False)
    state = FakeState("onboarding:step")

    ok = await FoodAIEnabledFilter()(msg, session, state)
    assert ok is False
    # No CTA should be sent while in FSM
    assert len(msg._answers) == 0


async def test_foodai_filter_skips_cta_when_fsm_active_photo():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    msg = FakeMessage(user_id=62)
    # Simulate photo message
    msg.photo = [object()]
    session = _FakeSessionFoodAI(is_premium=False, foodai_enabled=False)
    state = FakeState("onboarding:step")

    ok = await FoodAIEnabledFilter()(msg, session, state)
    assert ok is False
    # No CTA should be sent while in FSM
    assert len(msg._answers) == 0


async def test_foodai_filter_skips_cta_when_fsm_active_callback():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    cb = FakeCallbackQuery(data="foodai:save:1", user_id=63)
    session = _FakeSessionFoodAI(is_premium=False, foodai_enabled=False)
    state = FakeState("onboarding:step")

    ok = await FoodAIEnabledFilter()(cb, session, state)
    assert ok is False
    # No CTA and no callback.answer() while in FSM
    assert cb._answered is False
    assert len(cb.message._answers) == 0


# === Subscription grace window tests ===
async def test_is_subscription_active_grace_before_10_local(monkeypatch):
    from datetime import datetime, timezone
    import bot.services.users as users_mod

    # Expired at 06:00 UTC, now 06:30 UTC => expired; in MSK it's 09:30 (<10:00) => grace applies
    exp_utc = datetime(2025, 1, 10, 6, 0, tzinfo=timezone.utc)
    now_utc = datetime(2025, 1, 10, 6, 30, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now_utc
            try:
                return now_utc.astimezone(tz)
            except Exception:
                return now_utc

    class _FakeSessionSub:
        def __init__(self, exp, status="active"):
            self._exp = exp
            self._status = status
        async def execute(self, *args, **kwargs):
            exp = self._exp
            status = self._status
            class R:
                def first(self_inner):
                    return (exp, status)
                def scalars(self_inner):
                    class V:
                        def all(self_v):
                            return []
                    return V()
            return R()

    # Force user's tz to Europe/Moscow (UTC+3)
    async def _tz(session, user_id):
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Moscow")

    monkeypatch.setattr(users_mod, "get_user_tzinfo", _tz, raising=True)
    monkeypatch.setattr(users_mod, "datetime", _FixedDateTime, raising=True)

    session = _FakeSessionSub(exp_utc, "active")
    strict = await users_mod.is_subscription_active(session, 1, include_grace=False)
    with_grace = await users_mod.is_subscription_active(session, 1, include_grace=True)

    assert strict is False
    assert with_grace is True


async def test_is_subscription_active_grace_after_10_local(monkeypatch):
    from datetime import datetime, timezone
    import bot.services.users as users_mod

    # Expired at 06:00 UTC, now 07:30 UTC => 10:30 MSK (>10:00) => grace should NOT apply
    exp_utc = datetime(2025, 1, 10, 6, 0, tzinfo=timezone.utc)
    now_utc = datetime(2025, 1, 10, 7, 30, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now_utc
            try:
                return now_utc.astimezone(tz)
            except Exception:
                return now_utc

    class _FakeSessionSub:
        def __init__(self, exp, status="active"):
            self._exp = exp
            self._status = status
        async def execute(self, *args, **kwargs):
            exp = self._exp
            status = self._status
            class R:
                def first(self_inner):
                    return (exp, status)
                def scalars(self_inner):
                    class V:
                        def all(self_v):
                            return []
                    return V()
            return R()

    async def _tz(session, user_id):
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Moscow")

    monkeypatch.setattr(users_mod, "get_user_tzinfo", _tz, raising=True)
    monkeypatch.setattr(users_mod, "datetime", _FixedDateTime, raising=True)

    session = _FakeSessionSub(exp_utc, "active")
    with_grace = await users_mod.is_subscription_active(session, 1, include_grace=True)
    assert with_grace is False


# === Onboarding final:ok should skip sales for active subscribers (with grace) ===
async def test_onboarding_final_ok_routes_to_account_when_active(monkeypatch):
    import types
    import bot.handlers.onboarding as onb
    import bot.services.users as users_mod
    import bot.services.account as acc_svc
    import bot.handlers.account as acc_handlers

    # Patch DB session used inside handler
    class _FakeUser:
        def __init__(self):
            self.is_admin = False

    class _FakeSession:
        async def get(self, *args, **kwargs):
            return _FakeUser()
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeSessionmaker:
        def __call__(self, *args, **kwargs):
            return self
        async def __aenter__(self):
            return _FakeSession()
        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(onb, "sessionmaker", _FakeSessionmaker(), raising=True)

    async def _active(session, user_id, include_grace=False):
        return True
    # Handler imports is_subscription_active from services at runtime
    monkeypatch.setattr(users_mod, "is_subscription_active", _active, raising=True)

    async def _acc_text(user_id: int):
        return "ACCOUNT_SUMMARY"
    monkeypatch.setattr(acc_svc, "get_account_summary_text", _acc_text, raising=True)
    monkeypatch.setattr(acc_handlers, "_kb_account", lambda: object(), raising=False)

    # State with async clear()
    class _State:
        def __init__(self):
            self.cleared = False
        async def clear(self):
            self.cleared = True

    cb = FakeCallbackQuery(data="final:ok", user_id=777)
    state = _State()

    await onb.cb_final_ok(cb, state)

    # Should not show sales text; should send account summary
    texts = [t for t, _ in cb.message._answers]
    assert any("ACCOUNT_SUMMARY" in t for t in texts)
    assert not any("Секрет идеальной фигуры" in t for t in texts)
    assert state.cleared is True


# === FoodAI: no CTA on sale:* callbacks ===
async def test_foodai_filter_no_cta_on_sale_callbacks():
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    cb = FakeCallbackQuery(data="sale:choose", user_id=70)
    session = _FakeSessionFoodAI(is_premium=False, foodai_enabled=False)

    ok = await FoodAIEnabledFilter()(cb, session)
    assert ok is False
    # No CTA pushed into messages
    assert len(cb.message._answers) == 0


# === Admin bypass ===
async def test_foodai_filter_allows_admin(monkeypatch):
    from bot.filters.foodai_enabled import FoodAIEnabledFilter

    class _User:
        def __init__(self):
            self.is_admin = True
            self.foodai_enabled_at = None

    class _Sess:
        async def get(self, *args, **kwargs):
            return _User()

    msg = FakeMessage(user_id=71)
    ok = await FoodAIEnabledFilter()(msg, _Sess())
    assert ok is True
    assert len(msg._answers) == 0


async def test_onboarding_final_ok_routes_to_account_when_admin(monkeypatch):
    import bot.handlers.onboarding as onb
    import bot.services.users as users_mod
    import bot.services.account as acc_svc
    import bot.handlers.account as acc_handlers

    class _FakeUser:
        def __init__(self):
            self.is_admin = True

    class _FakeSession:
        async def get(self, *args, **kwargs):
            return _FakeUser()
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeSessionmaker:
        def __call__(self, *args, **kwargs):
            return self
        async def __aenter__(self):
            return _FakeSession()
        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(onb, "sessionmaker", _FakeSessionmaker(), raising=True)

    async def _inactive(session, user_id, include_grace=False):
        return False
    monkeypatch.setattr(users_mod, "is_subscription_active", _inactive, raising=True)

    async def _acc_text(user_id: int):
        return "ACCOUNT_SUMMARY"
    monkeypatch.setattr(acc_svc, "get_account_summary_text", _acc_text, raising=True)
    monkeypatch.setattr(acc_handlers, "_kb_account", lambda: object(), raising=False)

    class _State:
        def __init__(self):
            self.cleared = False
        async def clear(self):
            self.cleared = True

    cb = FakeCallbackQuery(data="final:ok", user_id=772)
    state = _State()

    await onb.cb_final_ok(cb, state)

    texts = [t for t, _ in cb.message._answers]
    assert any("ACCOUNT_SUMMARY" in t for t in texts)
    assert not any("Секрет идеальной фигуры" in t for t in texts)
    assert state.cleared is True


# === set_timezone scheduling respects subscription gating ===
async def test_set_timezone_schedules_only_when_active(monkeypatch):
    import bot.services.users as users_mod
    import bot.core.loader as loader_mod
    import bot.cache.redis as cache_redis_mod

    class _Sess:
        async def execute(self, *args, **kwargs):
            class R:
                def scalar_one_or_none(self):
                    return None
            return R()
        async def commit(self):
            return None

    called = {"zadd": 0}

    class _Redis:
        async def zadd(self, key, mapping):
            called["zadd"] += 1
            return True
        async def delete(self, *args, **kwargs):
            return 1

    # Force settings
    class _Settings:
        DAILY_REPORTS_ENABLED = True
        DAILY_REPORTS_REQUIRE_PREMIUM = True
        DAILY_REPORTS_HOUR = 8
        DAILY_REPORTS_JITTER_MIN = 0

    monkeypatch.setattr(users_mod.cfg, "settings", _Settings(), raising=False)
    fake_redis = _Redis()
    monkeypatch.setattr(users_mod, "redis_client", fake_redis, raising=True)
    monkeypatch.setattr(loader_mod, "redis_client", fake_redis, raising=False)
    monkeypatch.setattr(cache_redis_mod, "redis_client", fake_redis, raising=False)

    # Active -> schedules
    async def _active(session, user_id):
        return True
    monkeypatch.setattr(users_mod, "is_subscription_active", _active, raising=True)
    await users_mod.set_timezone(_Sess(), 1, "Europe/Moscow")
    assert called["zadd"] == 1


# === Daily reports: strict gating (no grace) ===
async def test_reports_strict_gating_not_active_skips(monkeypatch):
    import bot.services.reports as rep

    # Settings: require premium
    class _Settings:
        DAILY_REPORTS_REQUIRE_PREMIUM = True
    monkeypatch.setattr(rep, "settings", _Settings(), raising=False)

    # Patch sessionmaker used inside assemble_and_send_report
    class _Sess:
        def __init__(self):
            self.added = []
            self.updated = []
        async def scalar(self, *args, **kwargs):
            return None
        async def execute(self, *args, **kwargs):
            class R:
                def scalar_one_or_none(self):
                    return None
            return R()
        async def commit(self):
            return None
        async def rollback(self):
            return None
        def add(self, obj):
            self.added.append(obj)
    class _SM:
        def __call__(self, *args, **kwargs):
            return self
        async def __aenter__(self):
            return _Sess()
        async def __aexit__(self, exc_type, exc, tb):
            return False
    monkeypatch.setattr(rep, "sessionmaker", _SM(), raising=True)
    # Avoid DB access inside _fetch_plan_and_fact
    async def _fetch(user_id):
        from datetime import date as _date
        return (
            {"calories":1800,"protein_g":120.0,"fat_g":60.0,"carbs_g":200.0},
            {"calories":1700,"protein_g":100.0,"fat_g":55.0,"carbs_g":180.0},
            _date(2025, 1, 10),
        )
    monkeypatch.setattr(rep, "_fetch_plan_and_fact", _fetch, raising=True)

    # Patch subscription checker to inactive, and minimize heavy deps
    async def _inactive(session, user_id):
        return False
    monkeypatch.setattr(rep, "is_subscription_active", _inactive, raising=True)

    class _Bot:
        async def send_message(self, *args, **kwargs):
            raise AssertionError("send_message should not be called when not active")

    # Should early-return False and not send
    out = await rep.assemble_and_send_report(_Bot(), user_id=999, scheduled_epoch=None)
    assert out is False


async def test_reports_strict_gating_active_sends(monkeypatch):
    import bot.services.reports as rep

    class _Settings:
        DAILY_REPORTS_REQUIRE_PREMIUM = True
    monkeypatch.setattr(rep, "settings", _Settings(), raising=False)

    class _Sess:
        def __init__(self):
            self.added = []
        async def scalar(self, *args, **kwargs):
            return None
        async def execute(self, *args, **kwargs):
            class R:
                def scalar_one_or_none(self):
                    return None
            return R()
        async def commit(self):
            return None
        async def rollback(self):
            return None
        def add(self, obj):
            self.added.append(obj)
    class _SM:
        def __call__(self, *args, **kwargs):
            return self
        async def __aenter__(self):
            return _Sess()
        async def __aexit__(self, exc_type, exc, tb):
            return False
    monkeypatch.setattr(rep, "sessionmaker", _SM(), raising=True)

    async def _active(session, user_id):
        return True
    monkeypatch.setattr(rep, "is_subscription_active", _active, raising=True)
    async def _fetch(user_id):
        from datetime import date as _date
        return (
            {"calories":1800,"protein_g":120.0,"fat_g":60.0,"carbs_g":200.0},
            {"calories":1700,"protein_g":100.0,"fat_g":55.0,"carbs_g":180.0},
            _date(2025, 1, 10),
        )
    monkeypatch.setattr(rep, "_fetch_plan_and_fact", _fetch, raising=True)
    async def _ctx(uid):
        return None
    monkeypatch.setattr(rep, "_collect_user_context", _ctx, raising=True)
    monkeypatch.setattr(rep, "_pick_short_motivation", lambda : "short", raising=True)
    async def _gen(*args, **kwargs):
        return ("mot", "adv", None)
    monkeypatch.setattr(rep, "_gen_llm_content", _gen, raising=True)

    class _Bot:
        def __init__(self):
            self.sent = 0
        async def send_message(self, *args, **kwargs):
            self.sent += 1
            class M: message_id = 1
            return M()

    bot = _Bot()
    out = await rep.assemble_and_send_report(bot, user_id=1000, scheduled_epoch=None)
    assert out is True
    assert bot.sent == 1


async def test_reports_use_strict_gating_no_grace_flag(monkeypatch):
    import bot.services.reports as rep

    class _Settings:
        DAILY_REPORTS_REQUIRE_PREMIUM = True
    monkeypatch.setattr(rep, "settings", _Settings(), raising=False)

    seen = {"include_grace": None}
    async def _checker(session, user_id, include_grace=False):
        seen["include_grace"] = include_grace
        return False
    monkeypatch.setattr(rep, "is_subscription_active", _checker, raising=True)

    class _SM:
        def __call__(self, *args, **kwargs):
            return self
        async def __aenter__(self):
            class S:
                async def scalar(self, *args, **kwargs):
                    return None
                async def execute(self, *args, **kwargs):
                    class R:
                        def scalar_one_or_none(self):
                            return None
                    return R()
                async def commit(self):
                    return None
                async def rollback(self):
                    return None
                def add(self, obj):
                    return None
            return S()
        async def __aexit__(self, exc_type, exc, tb):
            return False
    monkeypatch.setattr(rep, "sessionmaker", _SM(), raising=True)
    # also avoid DB work in fetch
    async def _fetch2(user_id):
        from datetime import date as _date
        return (
            {"calories":1800,"protein_g":120.0,"fat_g":60.0,"carbs_g":200.0},
            {"calories":1700,"protein_g":100.0,"fat_g":55.0,"carbs_g":180.0},
            _date(2025, 1, 10),
        )
    monkeypatch.setattr(rep, "_fetch_plan_and_fact", _fetch2, raising=True)

    class _Bot:
        async def send_message(self, *args, **kwargs):
            raise AssertionError

    await rep.assemble_and_send_report(_Bot(), user_id=1, scheduled_epoch=None)
    assert seen["include_grace"] is False


# === Grace hour override ===
async def test_is_subscription_active_grace_hour_override(monkeypatch):
    from datetime import datetime, timezone
    import bot.services.users as users_mod

    # Override grace hour = 9
    class _Settings:
        SUBSCRIPTION_GRACE_HOUR = 9
        REBILL_HOUR = 10
    monkeypatch.setattr(users_mod.cfg, "settings", _Settings(), raising=False)

    exp_utc = datetime(2025, 1, 10, 6, 0, tzinfo=timezone.utc)
    now_utc = datetime(2025, 1, 10, 6, 30, tzinfo=timezone.utc)  # 09:30 MSK

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return now_utc
            try:
                return now_utc.astimezone(tz)
            except Exception:
                return now_utc

    class _FakeSessionSub:
        def __init__(self, exp, status="active"):
            self._exp = exp
            self._status = status
        async def execute(self, *args, **kwargs):
            exp = self._exp
            status = self._status
            class R:
                def first(self_inner):
                    return (exp, status)
                def scalars(self_inner):
                    class V:
                        def all(self_v):
                            return []
                    return V()
            return R()

    async def _tz(session, user_id):
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Moscow")

    monkeypatch.setattr(users_mod, "get_user_tzinfo", _tz, raising=True)
    monkeypatch.setattr(users_mod, "datetime", _FixedDateTime, raising=True)

    session = _FakeSessionSub(exp_utc, "active")
    # With override to 9: grace should NOT apply at 09:30
    assert await users_mod.is_subscription_active(session, 1, include_grace=True) is False


# =============================================================================
# === NEW: Tests for /templates, tpl:cat:*, FoodAI handlers, and catch-all gate
# =============================================================================


async def _patch_templates_sessionmaker(monkeypatch, exists: bool):
    import bot.handlers.templates as tpl
    monkeypatch.setattr(tpl, "sessionmaker", FakeSessionmaker(exists), raising=True)
    # Patch i18n
    monkeypatch.setattr(tpl, "_", lambda s, **kw: s.format(**kw) if "{" in s else s, raising=True)


async def _patch_gate_sessionmaker(monkeypatch, exists: bool):
    import bot.handlers.gate as gate
    monkeypatch.setattr(gate, "sessionmaker", FakeSessionmaker(exists), raising=True)
    monkeypatch.setattr(gate, "_", lambda s, **kw: s, raising=True)


async def _patch_foodai_sessionmaker(monkeypatch, exists: bool):
    import bot.handlers.foodai as foodai
    monkeypatch.setattr(foodai, "sessionmaker", FakeSessionmaker(exists), raising=True)
    monkeypatch.setattr(foodai, "_", lambda s, **kw: s.format(**kw) if "{" in s else s, raising=True)


# --- /templates gating ---
async def test_cmd_templates_gated_without_onboarding(monkeypatch):
    from bot.handlers.templates import cmd_templates

    await _patch_templates_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=100)

    await cmd_templates(m)
    # Expect exactly 1 CTA reply
    assert len(m._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_cmd_templates_allowed_with_onboarding(monkeypatch):
    from bot.handlers.templates import cmd_templates
    import bot.handlers.templates as tpl
    import bot.services.templates as tpl_svc

    await _patch_templates_sessionmaker(monkeypatch, exists=True)
    # Patch list_categories_with_counts to avoid real DB
    async def _counts(session, user_id):
        return {"breakfast": 0, "lunch": 0, "dinner": 0, "snack": 0}
    monkeypatch.setattr(tpl_svc, "list_categories_with_counts", _counts, raising=True)

    m = FakeMessage(user_id=101)
    await cmd_templates(m)
    # Should NOT contain onboarding CTA
    assert not any("онбординг" in t.lower() for t, _ in m._answers)
    # Should have some response (categories)
    assert len(m._answers) >= 1


# --- tpl:cat:* gating ---
async def test_cb_tpl_cat_gated_without_onboarding(monkeypatch):
    from bot.handlers.templates import cb_tpl_cat

    await _patch_templates_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="tpl:cat:breakfast", user_id=102)

    await cb_tpl_cat(cb)
    # Expect exactly 1 CTA reply
    assert len(cb.message._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)
    assert cb._answered is True


async def test_cb_tpl_cat_allowed_with_onboarding(monkeypatch):
    from bot.handlers.templates import cb_tpl_cat
    import bot.handlers.templates as tpl
    import bot.services.templates as tpl_svc

    await _patch_templates_sessionmaker(monkeypatch, exists=True)
    # Patch list_templates to avoid real DB
    async def _list(session, user_id, category):
        return []
    monkeypatch.setattr(tpl_svc, "list_templates", _list, raising=True)

    cb = FakeCallbackQuery(data="tpl:cat:lunch", user_id=103)
    # Patch edit_text to avoid TelegramAPIError
    cb.message.edit_text = cb.message.answer

    await cb_tpl_cat(cb)
    # Should NOT contain onboarding CTA
    assert not any("онбординг" in t.lower() for t, _ in cb.message._answers)


# --- Catch-all gate tests ---
async def test_gate_text_blocks_without_onboarding(monkeypatch):
    from bot.handlers.gate import gate_text

    await _patch_gate_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=200)
    m.text = "Привет, это текст"

    await gate_text(m)
    assert len(m._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_gate_text_passes_with_onboarding(monkeypatch):
    from bot.handlers.gate import gate_text

    await _patch_gate_sessionmaker(monkeypatch, exists=True)
    m = FakeMessage(user_id=201)
    m.text = "Привет, это текст"

    await gate_text(m)
    # No CTA, handler just returns (passes through to next router)
    assert len(m._answers) == 0


async def test_gate_photo_blocks_without_onboarding(monkeypatch):
    from bot.handlers.gate import gate_photo

    await _patch_gate_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=202)
    m.photo = [object()]

    await gate_photo(m)
    assert len(m._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_gate_sticker_blocks_without_onboarding(monkeypatch):
    from bot.handlers.gate import gate_sticker

    await _patch_gate_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=203)
    m.sticker = object()

    await gate_sticker(m)
    assert len(m._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_gate_video_blocks_without_onboarding(monkeypatch):
    from bot.handlers.gate import gate_video

    await _patch_gate_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=204)
    m.video = object()

    await gate_video(m)
    assert len(m._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in m._answers)


# --- FoodAI handler gating tests ---
class FakeState:
    def __init__(self, state_value=None):
        self._state = state_value

    async def get_state(self):
        return self._state

    async def set_state(self, state):
        self._state = state

    async def update_data(self, **kwargs):
        pass

    async def get_data(self):
        return {}


async def test_foodai_handle_food_photo_gated_without_onboarding(monkeypatch):
    from bot.handlers.foodai import handle_food_photo

    await _patch_foodai_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=300)
    m.photo = [type("Photo", (), {"file_id": "abc", "file_unique_id": "xyz"})()]
    state = FakeState(None)

    await handle_food_photo(m, state)
    # Expect exactly 1 CTA reply
    assert len(m._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_foodai_handle_food_text_gated_without_onboarding(monkeypatch):
    from bot.handlers.foodai import handle_food_text

    await _patch_foodai_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=301)
    m.text = "Съел яблоко"
    state = FakeState(None)

    await handle_food_text(m, state)
    # Expect exactly 1 CTA reply
    assert len(m._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in m._answers)


async def test_foodai_cb_save_gated_without_onboarding(monkeypatch):
    from bot.handlers.foodai import cb_foodai_save

    await _patch_foodai_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="foodai:save:123", user_id=302)
    state = FakeState(None)

    await cb_foodai_save(cb, state)
    # Expect exactly 1 CTA reply
    assert len(cb.message._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


async def test_foodai_cb_delete_gated_without_onboarding(monkeypatch):
    from bot.handlers.foodai import cb_foodai_delete

    await _patch_foodai_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="foodai:del:123", user_id=303)

    await cb_foodai_delete(cb)
    # Expect exactly 1 CTA reply
    assert len(cb.message._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


async def test_foodai_cb_edit_gated_without_onboarding(monkeypatch):
    from bot.handlers.foodai import cb_foodai_edit

    await _patch_foodai_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="foodai:edit:123", user_id=304)
    state = FakeState(None)

    await cb_foodai_edit(cb, state)
    # Expect exactly 1 CTA reply
    assert len(cb.message._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


async def test_foodai_cb_back_gated_without_onboarding(monkeypatch):
    from bot.handlers.foodai import cb_foodai_back

    await _patch_foodai_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="foodai:back:123", user_id=305)

    await cb_foodai_back(cb)
    # Expect exactly 1 CTA reply
    assert len(cb.message._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


async def test_foodai_cb_adjust_gated_without_onboarding(monkeypatch):
    from bot.handlers.foodai import cb_foodai_adjust

    await _patch_foodai_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="foodai:adj:cal:10:123", user_id=306)

    await cb_foodai_adjust(cb)
    # Expect exactly 1 CTA reply
    assert len(cb.message._answers) == 1
    assert any("онбординг" in t.lower() for t, _ in cb.message._answers)


# --- Ensure no duplicate CTAs (regression) ---
async def test_no_duplicate_cta_on_templates(monkeypatch):
    """Regression test: /templates should send exactly 1 CTA, not 2."""
    from bot.handlers.templates import cmd_templates

    await _patch_templates_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=400)

    await cmd_templates(m)
    # Count CTA messages
    cta_count = sum(1 for t, _ in m._answers if "онбординг" in t.lower())
    assert cta_count == 1, f"Expected 1 CTA, got {cta_count}"


async def test_no_duplicate_cta_on_tpl_cat(monkeypatch):
    """Regression test: tpl:cat:* should send exactly 1 CTA, not 2."""
    from bot.handlers.templates import cb_tpl_cat

    await _patch_templates_sessionmaker(monkeypatch, exists=False)
    cb = FakeCallbackQuery(data="tpl:cat:dinner", user_id=401)

    await cb_tpl_cat(cb)
    cta_count = sum(1 for t, _ in cb.message._answers if "онбординг" in t.lower())
    assert cta_count == 1, f"Expected 1 CTA, got {cta_count}"


async def test_no_duplicate_cta_on_arbitrary_text(monkeypatch):
    """Regression test: arbitrary text should send exactly 1 CTA."""
    from bot.handlers.gate import gate_text

    await _patch_gate_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=402)
    m.text = "Любой текст"

    await gate_text(m)
    cta_count = sum(1 for t, _ in m._answers if "онбординг" in t.lower())
    assert cta_count == 1, f"Expected 1 CTA, got {cta_count}"


async def test_no_duplicate_cta_on_photo(monkeypatch):
    """Regression test: photo should send exactly 1 CTA."""
    from bot.handlers.gate import gate_photo

    await _patch_gate_sessionmaker(monkeypatch, exists=False)
    m = FakeMessage(user_id=403)
    m.photo = [object()]

    await gate_photo(m)
    cta_count = sum(1 for t, _ in m._answers if "онбординг" in t.lower())
    assert cta_count == 1, f"Expected 1 CTA, got {cta_count}"
