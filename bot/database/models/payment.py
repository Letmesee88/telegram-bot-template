from __future__ import annotations
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at


class PaymentModel(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("subscriptions.id"), index=True)

    # YooKassa identifiers
    yk_payment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    idempotence_key: Mapped[str | None] = mapped_column(String(64))
    payment_method_id: Mapped[str | None] = mapped_column(String(128))

    # Money
    amount_value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="RUB")

    # Status
    status: Mapped[str] = mapped_column(
        Enum("pending", "succeeded", "canceled", "waiting_for_capture", name="payment_status"),
        nullable=False,
        server_default="pending",
    )

    description: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[created_at]
    captured_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
