from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, created_at


class MealModel(Base):
    __tablename__ = "meals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)

    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("TIMEZONE('utc', now())"), index=True
    )
    created_at: Mapped[created_at]
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=text("TIMEZONE('utc', now())"), onupdate=text("TIMEZONE('utc', now())")
    )

    title: Mapped[Optional[str]] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(Enum("photo", "text", "edit", name="meal_source"), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("draft", "saved", "deleted", name="meal_status"), nullable=False, server_default="draft"
    )

    calories: Mapped[Optional[int]]
    protein_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    fat_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    carbs_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    weight_g: Mapped[Optional[float]] = mapped_column(Numeric(8, 1))
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 2))

    analysis_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    references: Mapped[Optional[dict]] = mapped_column(JSONB)

    items: Mapped[list[MealItemModel]] = relationship(
        "MealItemModel", back_populates="meal", cascade="all, delete-orphan", lazy="selectin"
    )
    photos: Mapped[list[MealPhotoModel]] = relationship(
        "MealPhotoModel", back_populates="meal", cascade="all, delete-orphan", lazy="selectin"
    )


class MealItemModel(Base):
    __tablename__ = "meal_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meal_id: Mapped[int] = mapped_column(Integer, ForeignKey("meals.id", ondelete="CASCADE"), index=True, nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    weight_g: Mapped[Optional[float]] = mapped_column(Numeric(8, 1))
    calories: Mapped[Optional[float]] = mapped_column(Numeric(8, 1))
    protein_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    fat_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    carbs_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))

    meal: Mapped[MealModel] = relationship("MealModel", back_populates="items", lazy="selectin")


class MealPhotoModel(Base):
    __tablename__ = "meal_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meal_id: Mapped[int] = mapped_column(Integer, ForeignKey("meals.id", ondelete="CASCADE"), index=True, nullable=False)

    tg_file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    tg_file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    file_path: Mapped[Optional[str]] = mapped_column(String(512))

    meal: Mapped[MealModel] = relationship("MealModel", back_populates="photos", lazy="selectin")


class DailyIntakeModel(Base):
    __tablename__ = "daily_intake"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    date_utc: Mapped[datetime] = mapped_column(Date, nullable=False, index=True)

    calories: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    protein_g: Mapped[float] = mapped_column(Numeric(9, 1), nullable=False, default=0)
    fat_g: Mapped[float] = mapped_column(Numeric(9, 1), nullable=False, default=0)
    carbs_g: Mapped[float] = mapped_column(Numeric(9, 1), nullable=False, default=0)

    plan_calories: Mapped[Optional[int]] = mapped_column(Integer)

    created_at: Mapped[created_at]

    __table_args__ = (
        UniqueConstraint("user_id", "date_utc", name="uq_daily_intake_user_date"),
    )
