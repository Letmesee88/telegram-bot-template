from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.models.base import Base, big_int_pk, created_at


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[big_int_pk]
    first_name: Mapped[str]
    last_name: Mapped[str | None]
    username: Mapped[str | None]
    language_code: Mapped[str | None]
    referrer: Mapped[str | None]
    created_at: Mapped[created_at]

    is_admin: Mapped[bool] = mapped_column(default=False)
    is_suspicious: Mapped[bool] = mapped_column(default=False)
    is_block: Mapped[bool] = mapped_column(default=False)
    is_premium: Mapped[bool] = mapped_column(default=False)
    # Feature-flag activation timestamp for FoodAI (enabled when onboarding finished or subscription active)
    foodai_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-user timezone (IANA name, e.g., "Europe/Moscow"). If NULL, fall back to settings.DEFAULT_TZ
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
