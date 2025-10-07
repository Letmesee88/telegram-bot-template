from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, Numeric, String, DateTime, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, created_at


class MealTemplateModel(Base):
    __tablename__ = "meal_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)

    # Template category is immutable after creation
    category: Mapped[str] = mapped_column(
        Enum("breakfast", "lunch", "dinner", "snack", name="template_category"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)

    # Aggregated nutrition (optional)
    calories: Mapped[Optional[int]]
    protein_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    fat_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    carbs_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    weight_g: Mapped[Optional[float]] = mapped_column(Numeric(8, 1))

    created_at: Mapped[created_at]
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=text("TIMEZONE('utc', now())"), onupdate=text("TIMEZONE('utc', now())")
    )

    items: Mapped[list[MealTemplateItemModel]] = relationship(
        "MealTemplateItemModel", back_populates="template", cascade="all, delete-orphan", lazy="selectin"
    )


class MealTemplateItemModel(Base):
    __tablename__ = "meal_template_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("meal_templates.id", ondelete="CASCADE"), index=True, nullable=False
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    weight_g: Mapped[Optional[float]] = mapped_column(Numeric(8, 1))
    calories: Mapped[Optional[float]] = mapped_column(Numeric(8, 1))
    protein_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    fat_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))
    carbs_g: Mapped[Optional[float]] = mapped_column(Numeric(7, 1))

    template: Mapped[MealTemplateModel] = relationship("MealTemplateModel", back_populates="items", lazy="selectin")
