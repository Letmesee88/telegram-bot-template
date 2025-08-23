from __future__ import annotations

from typing import Optional
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.models.base import Base, created_at
from bot.database.models.user import UserModel  # Added import statement


class OnboardingAnswerModel(Base):
    __tablename__ = "onboarding_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    user: Mapped[UserModel] = relationship(UserModel, lazy="joined")  # Removed quotes around UserModel

    # Raw onboarding answers and computed plan stored as JSON
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    daily_plan: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Denormalized fields for fast admin filtering/sorting
    goal: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    calories: Mapped[Optional[int]] = mapped_column(Integer, index=True)

    created_at: Mapped[created_at]
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(), server_default=text("TIMEZONE('utc', now())"), onupdate=text("TIMEZONE('utc', now())")
    )
