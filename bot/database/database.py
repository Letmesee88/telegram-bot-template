from __future__ import annotations
from typing import TYPE_CHECKING
from uuid import uuid4

from asyncpg import Connection
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import bot.core.config as cfg

if TYPE_CHECKING:
    from sqlalchemy.engine.url import URL


class CConnection(Connection):  # type: ignore
    def _get_unique_id(self, prefix: str) -> str:
        return f"__asyncpg_{prefix}_{uuid4()}__"


def get_engine(url: URL | str | None = None) -> AsyncEngine:
    """Create a new AsyncEngine.

    When url is None, uses current settings.database_url (evaluated at call time).
    """
    url = url or cfg.settings.database_url
    return create_async_engine(
        url=url,
        echo=cfg.settings.DEBUG,
        poolclass=NullPool,
        connect_args={
            "connection_class": CConnection,
        },
    )


def get_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# --- Lazy singletons to avoid capturing stale env at import time ---
_engine_singleton: AsyncEngine | None = None
_sessionmaker_singleton: async_sessionmaker[AsyncSession] | None = None


def _ensure_engine() -> AsyncEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = get_engine()
    return _engine_singleton


def _ensure_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker_singleton
    if _sessionmaker_singleton is None:
        _sessionmaker_singleton = get_sessionmaker(_ensure_engine())
    return _sessionmaker_singleton


def sessionmaker() -> AsyncSession:
    """Factory returning a new AsyncSession.

    Usage: `async with sessionmaker() as session:`
    """
    return _ensure_sessionmaker()()
