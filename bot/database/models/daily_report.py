from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at


class DailyReportLogModel(Base):
    __tablename__ = "daily_reports_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)

    # Local date for which the report was generated
    date_local: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        Enum("queued", "sent", "failed", "skipped", name="daily_report_status"),
        nullable=False,
        server_default="queued",
    )

    message_id: Mapped[Optional[int]]
    sent_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    error_text: Mapped[Optional[str]] = mapped_column(String(512))
    retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[created_at]

    __table_args__ = (
        UniqueConstraint("user_id", "date_local", name="uq_daily_reports_user_date"),
    )
