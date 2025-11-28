from __future__ import annotations
from typing import TYPE_CHECKING
from datetime import datetime, timezone, timedelta, time as dtime
import random
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from bot.cache.redis import build_key, cached, clear_cache
from bot.database.models import UserModel, SubscriptionModel
import bot.core.config as cfg
from bot.core.loader import redis_client

if TYPE_CHECKING:
    from aiogram.types import User
    from sqlalchemy.ext.asyncio import AsyncSession


async def add_user(
    session: AsyncSession,
    user: User,
    referrer: str | None,
) -> None:
    """Add a new user to the database."""
    user_id: int = user.id
    first_name: str = user.first_name
    last_name: str | None = user.last_name
    username: str | None = user.username
    language_code: str | None = user.language_code

    # Auto-grant admin flag if user ID is listed in ADMIN_USER_IDS (does NOT imply premium)
    is_admin_env = user_id in cfg.settings.ADMIN_USER_IDS

    new_user = UserModel(
        id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        language_code=language_code,
        # App premium reflects only paid subscription, never Telegram Premium nor admin flag
        is_premium=False,
        referrer=referrer,
        is_admin=is_admin_env,
    )

    session.add(new_user)
    await session.commit()
    await clear_cache(user_exists, user_id)


@cached(key_builder=lambda session, user_id: build_key(user_id))
async def user_exists(session: AsyncSession, user_id: int) -> bool:
    """Checks if the user is in the database."""
    query = select(UserModel.id).filter_by(id=user_id).limit(1)

    result = await session.execute(query)

    user = result.scalar_one_or_none()
    return bool(user)


@cached(key_builder=lambda session, user_id: build_key(user_id))
async def get_first_name(session: AsyncSession, user_id: int) -> str:
    query = select(UserModel.first_name).filter_by(id=user_id)

    result = await session.execute(query)

    first_name = result.scalar_one_or_none()
    return first_name or ""


@cached(key_builder=lambda session, user_id: build_key(user_id))
async def get_language_code(session: AsyncSession, user_id: int) -> str:
    query = select(UserModel.language_code).filter_by(id=user_id)

    result = await session.execute(query)

    language_code = result.scalar_one_or_none()
    return language_code or ""


async def set_language_code(
    session: AsyncSession,
    user_id: int,
    language_code: str,
) -> None:
    stmt = update(UserModel).where(UserModel.id == user_id).values(language_code=language_code)

    await session.execute(stmt)
    await session.commit()


# =====================
# Timezone helpers
# =====================

@cached(key_builder=lambda session, user_id: build_key(user_id))
async def get_timezone(session: "AsyncSession", user_id: int) -> str:
    """Return user's IANA timezone string or empty string if not set."""
    query = select(UserModel.timezone).filter_by(id=user_id)
    result = await session.execute(query)
    tz = result.scalar_one_or_none()
    return tz or ""


async def set_timezone(session: "AsyncSession", user_id: int, tz_name: str) -> None:
    """Set user's timezone (IANA name). Caller must validate value upstream."""
    stmt = update(UserModel).where(UserModel.id == user_id).values(timezone=(tz_name or "").strip() or None)
    await session.execute(stmt)
    await session.commit()
    await clear_cache(get_timezone, user_id)
    # Immediately reschedule daily report for new timezone
    try:
        if getattr(cfg.settings, "DAILY_REPORTS_ENABLED", True):
            # Subscription gating: if premium required and user has no active subscription — do not schedule
            if getattr(cfg.settings, "DAILY_REPORTS_REQUIRE_PREMIUM", False):
                active = await is_subscription_active(session, user_id)
                if not active:
                    return
            # Compute next local 08:00 with jitter
            tzinfo = None
            try:
                if (tz_name or "").upper() in ("UTC", "Z"):
                    tzinfo = timezone.utc
                else:
                    tzinfo = ZoneInfo(tz_name)
            except Exception:
                tzinfo = timezone.utc
            now_local = datetime.now(tzinfo)
            target = datetime.combine(now_local.date(), dtime(int(getattr(cfg.settings, "DAILY_REPORTS_HOUR", 8) or 8), 0), tzinfo)
            if now_local >= target:
                target = target + timedelta(days=1)
            jitter_min = int(getattr(cfg.settings, "DAILY_REPORTS_JITTER_MIN", 60) or 60)
            target = target + timedelta(minutes=random.randint(0, max(0, jitter_min)))
            epoch = int(target.astimezone(timezone.utc).timestamp())
            await redis_client.zadd("reports:schedule", {user_id: epoch})
    except Exception:
        # Best-effort; failure here should not break user flow
        pass


async def get_user_tzinfo(session: "AsyncSession", user_id: int):
    """Resolve user's tzinfo with fallback to DEFAULT_TZ or UTC."""
    tz_name = str(getattr(cfg.settings, "DEFAULT_TZ", "Europe/Moscow") or "Europe/Moscow")
    try:
        user_tz = await get_timezone(session, user_id)
        if user_tz:
            tz_name = user_tz
    except Exception:
        pass
    try:
        if (tz_name or "").upper() in ("UTC", "Z"):
            return timezone.utc
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


async def today_local_utc_dates(session: "AsyncSession", user_id: int) -> set:
    """Return 1-2 UTC dates covering user's local 'today' window."""
    tz = await get_user_tzinfo(session, user_id)
    now_local = datetime.now(tz)
    local_date = now_local.date()
    local_start = datetime.combine(local_date, dtime(0, 0), tz)
    local_end = local_start + timedelta(days=1)
    d1 = local_start.astimezone(timezone.utc).date()
    d2 = (local_end - timedelta(seconds=1)).astimezone(timezone.utc).date()
    return {d1, d2}


@cached(key_builder=lambda session, user_id: build_key(user_id))
async def is_admin(session: AsyncSession, user_id: int) -> bool:
    # ENV-based superadmin: bypass DB if user is listed in ADMIN_USER_IDS
    if user_id in cfg.settings.ADMIN_USER_IDS:
        return True

    query = select(UserModel.is_admin).filter_by(id=user_id)

    result = await session.execute(query)

    is_admin = result.scalar_one_or_none()
    return bool(is_admin)


async def set_is_admin(session: AsyncSession, user_id: int, is_admin: bool) -> None:
    if is_admin:
        # Adminship does not alter premium status
        stmt = update(UserModel).where(UserModel.id == user_id).values(is_admin=True)
        await session.execute(stmt)
    else:
        stmt = update(UserModel).where(UserModel.id == user_id).values(is_admin=False)
        await session.execute(stmt)
    await session.commit()
    # Invalidate cached admin flag so changes are visible immediately
    await clear_cache(is_admin, user_id)


@cached(key_builder=lambda session: build_key())
async def get_all_users(session: AsyncSession) -> list[UserModel]:
    query = select(UserModel)

    result = await session.execute(query)

    users = result.scalars()
    return list(users)


@cached(key_builder=lambda session: build_key())
async def get_user_count(session: AsyncSession) -> int:
    query = select(func.count()).select_from(UserModel)

    result = await session.execute(query)

    count = result.scalar_one_or_none() or 0
    return int(count)


async def is_subscription_active(session: "AsyncSession", user_id: int, include_grace: bool = False) -> bool:
    """Return True if user has an active, non-expired app subscription.

    Strict criteria (default):
    - subscriptions.status == 'active'
    - subscriptions.expires_at_utc > now (UTC)

    If include_grace is True, also considers a local-time grace window until HH:00
    on the calendar day of expiry. HH comes from settings.SUBSCRIPTION_GRACE_HOUR (fallback REBILL_HOUR, default 10).
    """
    now_utc = datetime.now(timezone.utc)
    res = await session.execute(
        select(SubscriptionModel.expires_at_utc, SubscriptionModel.status)
        .where(SubscriptionModel.user_id == user_id)
        .limit(1)
    )
    row = res.first()
    if not row:
        return False
    expires_at_utc, status = row
    if status != "active" or not expires_at_utc:
        return False
    if expires_at_utc > now_utc:
        return True
    if not include_grace:
        return False
    # Grace: allow access until HH:00 local time on the expiry date
    try:
        grace_hour = None
        try:
            grace_hour = int(getattr(cfg.settings, "SUBSCRIPTION_GRACE_HOUR", None) or getattr(cfg.settings, "REBILL_HOUR", 10) or 10)
        except Exception:
            grace_hour = 10
        tz = await get_user_tzinfo(session, user_id)
        now_local = datetime.now(tz)
        exp_local = expires_at_utc.astimezone(tz)
        if (now_local.date() == exp_local.date()) and (now_local.hour < int(grace_hour)):
            return True
    except Exception:
        return False
    return False
