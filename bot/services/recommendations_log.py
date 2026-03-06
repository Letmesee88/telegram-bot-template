from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from bot.database.models import RecommendationLogModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def add_recommendation_title(session: AsyncSession, user_id: int, title: str) -> None:
    """Persist recommended title for anti-repeat window.

    We keep raw events; deduplication is handled at read time (distinct titles),
    and anti-repeat relies on time window filtering.
    """
    row = RecommendationLogModel(user_id=user_id, title=title.strip())
    session.add(row)
    await session.commit()


async def get_recent_titles(
    session: AsyncSession,
    user_id: int,
    *,
    window_days: int = 3,
    limit: int = 20,
) -> list[str]:
    """Return recent titles for user within window, most recent first, unique-ordered.

    Implementation: fetch last N rows within window and then stable-unique by order.
    """
    since = datetime.now(timezone.utc) - timedelta(days=max(0, int(window_days)))

    stmt = (
        select(RecommendationLogModel.title)
        .where(
            (RecommendationLogModel.user_id == user_id)
            & (RecommendationLogModel.ts >= since)
        )
        .order_by(RecommendationLogModel.ts.desc())
        .limit(max(1, int(limit)))
    )
    result = await session.execute(stmt)
    rows = [r[0] for r in result.fetchall()]

    # Make unique in order
    seen: set[str] = set()
    out: list[str] = []
    for t in rows:
        tt = (t or "").strip().lower()
        if tt and tt not in seen:
            seen.add(tt)
            out.append(tt)
            if len(out) >= limit:
                break
    return out
