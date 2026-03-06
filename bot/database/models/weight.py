from __future__ import annotations
from datetime import date, datetime
from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at


class WeightLogModel(Base):
    __tablename__ = "weight_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)

    weight_kg: Mapped[float] = mapped_column(Numeric(5, 1), nullable=False)
    # UTC timestamp when the record was made
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("TIMEZONE('utc', now())"), nullable=False, index=True)
    # Local calendar day for user at the moment of recording
    recorded_local_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    source: Mapped[str | None] = mapped_column(String(32), nullable=True, server_default=text("'manual'"))
    created_at: Mapped[created_at]

    __table_args__ = (
        UniqueConstraint("user_id", "recorded_local_date", name="uq_weight_user_local_date"),
    )
