from __future__ import annotations

from time import perf_counter

from aiogram import Router, types
from aiogram.filters import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.loader import redis_client
from bot.filters.admin import AdminFilter
from bot.services.users import get_user_count


router = Router(name="admin")


@router.message(Command(commands=["ping"]), AdminFilter())
async def ping_handler(message: types.Message, session: AsyncSession) -> None:
    """Healthcheck for admins: reports latency and checks DB/Redis connectivity."""
    t0 = perf_counter()

    # DB check
    db_ok = True
    try:
        await session.execute(select(1))
    except Exception:
        db_ok = False

    # Redis check
    redis_ok = True
    try:
        pong = await redis_client.ping()
        redis_ok = bool(pong)
    except Exception:
        redis_ok = False

    latency_ms = int((perf_counter() - t0) * 1000)
    status = "OK" if (db_ok and redis_ok) else "DEGRADED"

    text = (
        f"pong: {status}\n"
        f"latency: {latency_ms} ms\n"
        f"DB: {'OK' if db_ok else 'FAIL'}\n"
        f"Redis: {'OK' if redis_ok else 'FAIL'}"
    )
    await message.answer(text)


@router.message(Command(commands=["stats"]), AdminFilter())
async def stats_handler(message: types.Message, session: AsyncSession) -> None:
    """Basic stats for admins."""
    users_total = await get_user_count(session)

    text = (
        "📊 Stats\n"
        f"Users: {users_total}\n"
    )
    await message.answer(text)
