import pytest
from datetime import datetime, timedelta, timezone, date

import types


# ---- Local fakes (lightweight; do not import across tests) ----
class FakeResult:
    def __init__(self, scalar_obj=None, scalars_list=None):
        self._scalar_obj = scalar_obj
        self._scalars_list = scalars_list or []

    def scalar_one_or_none(self):
        return self._scalar_obj

    class _Scalars:
        def __init__(self, items):
            self._items = list(items)
        def first(self):
            return self._items[0] if self._items else None
        def all(self):
            return list(self._items)
    def scalars(self):
        return FakeResult._Scalars(self._scalars_list)


class SelectPromise:
    def __init__(self, model):
        self.model = model
    def where(self, *args, **kwargs):
        return self
    def limit(self, *args, **kwargs):
        return self
    def join(self, *args, **kwargs):
        return self
    def distinct(self, *args, **kwargs):
        return self


def fake_select(model):
    return SelectPromise(model)


class UpdatePromise:
    def __init__(self, model):
        self.model = model
        self._values = {}
    def where(self, *args, **kwargs):
        return self
    def values(self, **values):
        self._values = dict(values)
        return self


def fake_update(model):
    return UpdatePromise(model)


class FakeSession:
    def __init__(self, state: dict):
        self.state = state
        self.added = []
        self.updates = []
        self._commits = 0

    async def execute(self, query):
        # SELECT
        if isinstance(query, SelectPromise):
            model_name = getattr(query.model, "__name__", None)
            model_name = model_name or str(query.model)
            if "DailyIntakeModel" in model_name:
                # Return 1 row if active flag set (use scalars_list for .scalars().first())
                if self.state.get("intake_active", False):
                    return FakeResult(scalars_list=[types.SimpleNamespace(id=1)])
                return FakeResult(scalars_list=[])
            if "DailyReportLogModel" in model_name:
                return FakeResult(scalar_obj=self.state.get("log_existing"))
            if "OnboardingAnswerModel" in model_name:
                # Seed base query returns active user_ids
                uids = self.state.get("seed_active_uids", [])
                return FakeResult(scalars_list=uids)
        # UPDATE
        if isinstance(query, UpdatePromise):
            self.updates.append((query.model, dict(query._values)))
            return FakeResult()
        return FakeResult()

    async def scalar(self, query):
        if isinstance(query, SelectPromise):
            model_name = getattr(query.model, "__name__", str(query.model))
            if model_name.endswith("DailyReportLogModel"):
                return self.state.get("log_existing")
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self._commits += 1

    async def rollback(self):
        return None


class FakeSessionCM:
    def __init__(self, session: FakeSession):
        self._s = session
    async def __aenter__(self):
        return self._s
    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeBot:
    def __init__(self):
        self.sent = []
    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return None


class FakeRedis:
    def __init__(self):
        self.zsets = {}
    async def zadd(self, key, mapping: dict, nx: bool = False):
        z = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            if nx and str(member) in z:
                continue
            z[str(member)] = int(score)
    class _Pipe:
        def __init__(self, outer):
            self.outer = outer
            self.ops = []
        def zscore(self, key, member):
            self.ops.append(("zscore", key, member))
        def zadd(self, key, mapping: dict, nx: bool = False):
            self.ops.append(("zadd", key, mapping, nx))
        async def execute(self):
            out = []
            for op in self.ops:
                if op[0] == "zscore":
                    _, key, member = op
                    z = self.outer.zsets.get(key, {})
                    out.append(z.get(str(member)))
                elif op[0] == "zadd":
                    _, key, mapping, nx = op
                    await self.outer.zadd(key, mapping, nx=nx)
            return out
    def pipeline(self, transaction=False):
        return FakeRedis._Pipe(self)


# ---- Tests ----

@pytest.mark.asyncio
async def test_runtime_skip_inactive(monkeypatch):
    from bot.services import reports as rep
    # Config: require activity, disable premium gate
    monkeypatch.setattr(rep.settings, "DAILY_REPORTS_REQUIRE_ACTIVITY_DAYS", 7)
    monkeypatch.setattr(rep.settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False)

    # Fake tz
    async def tzinfo(session, user_id):
        return timezone.utc
    monkeypatch.setattr(rep, "get_user_tzinfo", tzinfo)

    # Stub plan/fact fetch to avoid DB
    y_local = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    async def fake_fetch(uid: int):
        return ({"calories": 0}, {"calories": 0}, y_local)
    monkeypatch.setattr(rep, "_fetch_plan_and_fact", fake_fetch)

    # Fake SQL toolkit and session
    state = {
        "intake_active": False,
        "log_existing": None,
    }
    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))
    monkeypatch.setattr(rep, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(rep, "select", fake_select)
    monkeypatch.setattr(rep, "update", fake_update)

    bot = FakeBot()
    should_reschedule = await rep.assemble_and_send_report(bot, user_id=123)
    assert should_reschedule is False
    # And no messages sent
    assert len(bot.sent) == 0


@pytest.mark.asyncio
async def test_runtime_active_allows_send(monkeypatch):
    from bot.services import reports as rep
    monkeypatch.setattr(rep.settings, "DAILY_REPORTS_REQUIRE_ACTIVITY_DAYS", 7)
    monkeypatch.setattr(rep.settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False)

    async def tzinfo(session, user_id):
        return timezone.utc
    monkeypatch.setattr(rep, "get_user_tzinfo", tzinfo)

    y_local = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    async def fake_fetch(uid: int):
        return ({"calories": 0}, {"calories": 0}, y_local)
    monkeypatch.setattr(rep, "_fetch_plan_and_fact", fake_fetch)

    # Make activity present
    state = {
        "intake_active": True,
        "log_existing": None,
    }
    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))
    monkeypatch.setattr(rep, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(rep, "select", fake_select)
    monkeypatch.setattr(rep, "update", fake_update)

    # Avoid real LLM: stub generator
    async def fake_llm(plan, fact, ctx):
        return ("mot", "adv", None)
    monkeypatch.setattr(rep, "_gen_llm_content", fake_llm)
    # Stub context collector
    async def fake_ctx(uid: int):
        return {}
    monkeypatch.setattr(rep, "_collect_user_context", fake_ctx)

    bot = FakeBot()
    res = await rep.assemble_and_send_report(bot, user_id=456)
    # Should return True (to reschedule) or None (if function uses implicit)
    assert res in (True, None)
    # And at least one message sent
    assert len(bot.sent) >= 1


@pytest.mark.asyncio
async def test_seed_audience_activity_filter(monkeypatch):
    from bot.background import report_scheduler as sched
    # Enable activity filter; ignore premium gate for this test
    monkeypatch.setattr(sched.settings, "DAILY_REPORTS_REQUIRE_ACTIVITY_DAYS", 7)
    monkeypatch.setattr(sched.settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False)

    # Fixed epoch for determinism
    async def next_epoch(uid: int):
        return 1728000000
    monkeypatch.setattr(sched, "_next_run_epoch", next_epoch)

    # Fake session to return active uids [101, 202]
    state = {"seed_active_uids": [101, 202]}
    def fake_sessionmaker():
        return FakeSessionCM(FakeSession(state))
    monkeypatch.setattr(sched, "sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(sched, "select", fake_select)

    # Fake Redis with pipeline support
    fr = FakeRedis()
    monkeypatch.setattr(sched, "redis_client", fr)

    await sched._seed_audience()
    z = fr.zsets.get(sched.ZSET_KEY, {})
    # Both active users should be scheduled
    assert "101" in z and z["101"] == 1728000000
    assert "202" in z and z["202"] == 1728000000
