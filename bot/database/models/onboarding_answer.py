from __future__ import annotations
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.database.models.base import Base, created_at
from bot.database.models.user import UserModel  # Added import statement


class OnboardingAnswerModel(Base):
    __tablename__ = "onboarding_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    user: Mapped[UserModel] = relationship(UserModel, lazy="joined")  # Removed quotes around UserModel

    # Raw onboarding answers and computed plan stored as json
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    daily_plan: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Denormalized fields for fast admin filtering/sorting
    goal: Mapped[str | None] = mapped_column(String(16), index=True)
    calories: Mapped[int | None] = mapped_column(Integer, index=True)

    created_at: Mapped[created_at]
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(), server_default=text("TIMEZONE('utc', now())"), onupdate=text("TIMEZONE('utc', now())")
    )
