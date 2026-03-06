from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.config import settings
from bot.database.models import PaymentModel, SubscriptionModel, UserModel


def trial_days() -> int:
    days = int(getattr(settings, "FREE_TRIAL_DAYS", 3) or 3)
    return max(1, days)


async def is_free_trial_available(session: AsyncSession, user_id: int) -> bool:
    """Return True if user can still start the free trial."""
    existing_sub = await session.scalar(
        select(SubscriptionModel.id).where(SubscriptionModel.user_id == user_id).limit(1)
    )
    if existing_sub is not None:
        return False

    rows = (
        await session.execute(
            select(PaymentModel.meta)
            .where(PaymentModel.user_id == user_id, PaymentModel.status == "succeeded")
            .order_by(PaymentModel.id.desc())
            .limit(50)
        )
    ).scalars().all()
    for md in rows:
        if str((md or {}).get("plan", "")).lower() == "trial":
            return False
    return True


async def activate_free_trial(session: AsyncSession, user_id: int) -> datetime | None:
    """Activate free trial and return expiry in UTC; None means trial is unavailable."""
    if not await is_free_trial_available(session, user_id):
        return None

    now_utc = datetime.now(timezone.utc)
    expires_at = now_utc + timedelta(days=trial_days())

    sub = SubscriptionModel(
        user_id=user_id,
        status="active",
        plan="trial",
        payment_method_id=None,
        auto_renew=False,
        next_plan=None,
        started_at_utc=now_utc,
        expires_at_utc=expires_at,
    )
    session.add(sub)
    await session.execute(
        update(UserModel).where(UserModel.id == user_id).values(is_premium=True)
    )
    await session.execute(
        update(UserModel)
        .where(UserModel.id == user_id, UserModel.foodai_enabled_at.is_(None))
        .values(foodai_enabled_at=func.now())
    )
    await session.commit()
    return expires_at
