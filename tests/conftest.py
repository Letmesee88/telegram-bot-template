from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from typing import Generator

import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# ---------- pytest-docker configuration ----------


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig) -> str:
    # Path to tests/compose/docker-compose.yml
    return str(pytestconfig.rootpath / "tests" / "compose" / "docker-compose.yml")


@pytest.fixture(scope="session")
def docker_compose_files(pytestconfig) -> list[str]:
    # Some pytest-docker versions expect a list fixture
    return [str(pytestconfig.rootpath / "tests" / "compose" / "docker-compose.yml")]


@pytest.fixture(scope="session")
def _pg_ready_checker() -> callable:
    def check(host: str, port: int) -> bool:
        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                user="postgres",
                password="postgres",
                dbname="testdb",
                connect_timeout=1,
            )
            conn.close()
            return True
        except Exception:
            return False

    return check


@pytest.fixture(scope="session")
def postgres_service(docker_ip, docker_services, _pg_ready_checker) -> tuple[str, int]:
    port = docker_services.port_for("postgres", 5432)

    # Wait until responsive
    docker_services.wait_until_responsive(
        timeout=60.0,
        pause=1.0,
        check=lambda: _pg_ready_checker(docker_ip, port),
    )
    return docker_ip, port


# ---------- Test DB bootstrap (env + migrations) ----------


@pytest.fixture(scope="session")
def test_db_env(postgres_service) -> dict[str, str]:
    host, port = postgres_service
    env = {
        "DB_HOST": host,
        "DB_PORT": str(port),
        "DB_USER": "postgres",
        "DB_PASS": "postgres",
        "DB_NAME": "testdb",
        # Avoid webhook binding during any imports
        "USE_WEBHOOK": "0",
        # Disable DEBUG noise
        "DEBUG": "0",
        # Make analytics in /start synchronous during tests to avoid race conditions
        "ANALYTICS_SYNC_START": "1",
        # Required by settings, but not used in tests
        # Use a string that matches Telegram token pattern to avoid aiogram validation errors
        "BOT_TOKEN": "123456:TESTTESTTESTTESTTESTTESTTESTTE",
        "AMPLITUDE_API_KEY": "TEST",
        # Redis not used in these tests
        "REDIS_HOST": "localhost",
        "REDIS_PORT": "6379",
        # Enable YooKassa configuration branch
        "YOOKASSA_SHOP_ID": "1",
        "YOOKASSA_SECRET_KEY": "TEST",
    }
    # Set env before importing app modules
    os.environ.update(env)
    # Force-refresh runtime settings so any prior imports don't keep stale values from .env
    try:
        import bot.core.config as cfg  # type: ignore
        cfg.settings = cfg.Settings()  # type: ignore[assignment]
    except Exception:
        pass
    # Reset DB engine/sessionmaker singletons to pick up new settings
    try:
        import bot.database.database as db  # type: ignore
        if hasattr(db, "_engine_singleton"):
            db._engine_singleton = None  # type: ignore[attr-defined]
        if hasattr(db, "_sessionmaker_singleton"):
            db._sessionmaker_singleton = None  # type: ignore[attr-defined]
    except Exception:
        pass
    return env


@pytest.fixture(scope="session")
def apply_migrations(test_db_env) -> None:
    # Run Alembic migrations against the ephemeral DB
    cfg = Config("alembic.ini")
    # env.py will read settings.database_url which uses env we set above
    command.upgrade(cfg, "head")


# ---------- Async SQLAlchemy engine/session for tests ----------


@pytest.fixture(scope="session")
async def async_engine(apply_migrations) -> AsyncGenerator[AsyncEngine, None]:
    # Import after env is set and migrations applied
    from bot.database.database import get_engine
    from bot.core.config import settings

    engine = get_engine(url=settings.database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
async def async_sessionmaker(async_engine: AsyncEngine) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    from bot.database.database import get_sessionmaker

    sm = get_sessionmaker(async_engine)
    yield sm


@pytest.fixture
async def db_session(async_sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    async with async_sessionmaker() as session:
        yield session
        # Rollback any uncommitted changes between tests
        await session.rollback()


@pytest.fixture
async def ensure_user(db_session):
    from bot.database.models import UserModel

    async def _make(
        user_id: int = 10001,
        first_name: str = "Test",
        last_name: str | None = None,
        username: str | None = None,
        language_code: str = "ru",
    ) -> int:
        await db_session.merge(
            UserModel(
                id=user_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
                language_code=language_code,
            )
        )
        await db_session.commit()
        return user_id

    return _make


# ---------- Minimal in-memory async Redis stub for tests ----------


class _FakeRedis:
    def __init__(self) -> None:
        self._kv: dict[str, str] = {}
        self._z: dict[str, dict[str, int]] = {}

    # String ops
    async def set(self, key: str, value: str, nx: bool | None = None, ex: int | None = None):
        if nx:
            if key in self._kv:
                return False
        self._kv[key] = str(value)
        return True

    async def get(self, key: str):
        return self._kv.get(key)

    async def exists(self, key: str) -> int:
        return 1 if key in self._kv else 0

    async def delete(self, key: str) -> None:
        self._kv.pop(key, None)

    async def expire(self, key: str, seconds: int) -> None:  # no-op for tests
        return None

    async def incr(self, key: str) -> int:
        cur = int(self._kv.get(key) or 0)
        cur += 1
        self._kv[key] = str(cur)
        return cur

    # ZSET ops
    async def zadd(self, key: str, mapping: dict[str, int], nx: bool | None = None):
        z = self._z.setdefault(key, {})
        for member, score in mapping.items():
            if nx and member in z:
                continue
            z[member] = int(score)
        return True

    async def zrangebyscore(self, key: str, min: str | int, max: int, start: int = 0, num: int = 200, withscores: bool = False):
        z = self._z.get(key, {})
        items = [(m, s) for m, s in z.items() if (min == "-inf" or s >= int(min)) and s <= int(max)]
        items.sort(key=lambda x: x[1])
        sliced = items[start:start + num]
        if withscores:
            return sliced
        return [m for m, _ in sliced]

    async def zrem(self, key: str, member: str) -> None:
        z = self._z.get(key, {})
        z.pop(member, None)

    # Pipeline stub
    def pipeline(self, transaction: bool = False):
        self._pipe_buf: list[tuple[str, tuple, dict]] = []
        return self

    def zadd_pipe(self, key: str, mapping: dict[str, int], nx: bool | None = None):
        self._pipe_buf.append(("zadd", (key, mapping), {"nx": nx}))
        return self

    async def execute(self):
        for op, args, kwargs in getattr(self, "_pipe_buf", []):
            if op == "zadd":
                await self.zadd(*args, **kwargs)
        self._pipe_buf = []
        return True


@pytest.fixture(autouse=True)
async def patch_redis_client(monkeypatch):
    # Replace global redis_client with in-memory stub for all tests
    from bot.core import loader
    fake = _FakeRedis()
    monkeypatch.setattr(loader, "redis_client", fake, raising=False)
    yield fake


@pytest.fixture
def capture_bot_messages(monkeypatch):
    from bot.core.loader import bot
    sent: list[tuple[int, str]] = []

    async def _fake_send_message(user_id: int, text: str, *args, **kwargs):
        sent.append((user_id, text))

    monkeypatch.setattr(bot, "send_message", _fake_send_message, raising=True)
    return sent


@pytest.fixture
def yk_stub(monkeypatch):
    # Helper to patch Payment.find_one with a stub object
    class _PM:
        def __init__(self, id: str, saved: bool = True) -> None:
            self.id = id
            self.saved = saved

    class _Amount:
        def __init__(self, value: str, currency: str = "RUB") -> None:
            self.value = value
            self.currency = currency

    class _PaymentObj:
        def __init__(self, *, status: str, value: str, metadata: dict, pm_id: str, pm_saved: bool = True) -> None:
            self.status = status
            self.amount = _Amount(value, "RUB")
            self.metadata = metadata
            self.payment_method = _PM(pm_id, pm_saved)

    from yookassa import Payment as _YP

    def _make(status: str, value: str, metadata: dict, pm_id: str, pm_saved: bool = True):
        obj = _PaymentObj(status=status, value=value, metadata=metadata, pm_id=pm_id, pm_saved=pm_saved)

        def _find_one(_payment_id: str):
            return obj

        monkeypatch.setattr(_YP, "find_one", _find_one, raising=True)
        return obj

    return _make


@pytest.fixture
def make_webhook_request():
    # Build a minimal fake request object for YooKassaWebhookView
    class _Req:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        async def json(self) -> dict:
            return self._payload

    return lambda payload: _Req(payload)


@pytest.fixture
def make_yk_view():
    from bot.handlers.yookassa_webhook import YooKassaWebhookView

    class _View(YooKassaWebhookView):
        def __init__(self, req):
            self._request = req

        @property
        def request(self):
            return self._request

    return lambda req: _View(req)
