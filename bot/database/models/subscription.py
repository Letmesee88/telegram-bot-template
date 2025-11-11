from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at


class SubscriptionModel(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)

    status: Mapped[str] = mapped_column(
        Enum("active", "past_due", "canceled", name="subscription_status"),
        nullable=False,
        server_default="active",
    )
    plan: Mapped[str] = mapped_column(
        Enum("trial", "month", "year", name="subscription_plan"),
        nullable=False,
        server_default="month",
    )

    payment_method_id: Mapped[Optional[str]] = mapped_column(String(128))

    started_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("TIMEZONE('utc', now())"), nullable=False
    )
    expires_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    canceled_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[created_at]
