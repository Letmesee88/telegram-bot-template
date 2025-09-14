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
