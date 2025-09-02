"""FoodAI models and users.foodai_enabled_at

Revision ID: f0oda1f00d01
Revises: b7f3e2c9d1a0
Create Date: 2025-09-02 20:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f0oda1f00d01"
down_revision: Union[str, None] = "b7f3e2c9d1a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


meal_source = postgresql.ENUM("photo", "text", "edit", name="meal_source", create_type=False)
meal_status = postgresql.ENUM("draft", "saved", "deleted", name="meal_status", create_type=False)


def upgrade() -> None:
    # Enums (idempotent creation)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'meal_source') THEN
                CREATE TYPE meal_source AS ENUM ('photo', 'text', 'edit');
            END IF;
        END$$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'meal_status') THEN
                CREATE TYPE meal_status AS ENUM ('draft', 'saved', 'deleted');
            END IF;
        END$$;
        """
    )

    # users.foodai_enabled_at (add if missing)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {c["name"] for c in inspector.get_columns("users")}
    if "foodai_enabled_at" not in user_columns:
        op.add_column(
            "users",
            sa.Column("foodai_enabled_at", sa.DateTime(timezone=True), nullable=True),
        )

    # meals
    if not inspector.has_table("meals"):
        op.create_table(
            "meals",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("TIMEZONE('utc', now())"), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("source", meal_source, nullable=False),
            sa.Column("status", meal_status, nullable=False, server_default="draft"),
            sa.Column("calories", sa.Integer(), nullable=True),
            sa.Column("protein_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("fat_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("carbs_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("weight_g", sa.Numeric(8, 1), nullable=True),
            sa.Column("confidence", sa.Numeric(4, 2), nullable=True),
            sa.Column("analysis_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("references", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_meals_user_id ON meals (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_meals_consumed_at ON meals (consumed_at)")

    # meal_items
    if not inspector.has_table("meal_items"):
        op.create_table(
            "meal_items",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("meal_id", sa.Integer(), sa.ForeignKey("meals.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("weight_g", sa.Numeric(8, 1), nullable=True),
            sa.Column("calories", sa.Numeric(8, 1), nullable=True),
            sa.Column("protein_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("fat_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("carbs_g", sa.Numeric(7, 1), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_meal_items_meal_id ON meal_items (meal_id)")

    # meal_photos
    if not inspector.has_table("meal_photos"):
        op.create_table(
            "meal_photos",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("meal_id", sa.Integer(), sa.ForeignKey("meals.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tg_file_id", sa.String(length=256), nullable=False),
            sa.Column("tg_file_unique_id", sa.String(length=128), nullable=False),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("file_path", sa.String(length=512), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_meal_photos_meal_id ON meal_photos (meal_id)")

    # daily_intake
    if not inspector.has_table("daily_intake"):
        op.create_table(
            "daily_intake",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("date_utc", sa.Date(), nullable=False),
            sa.Column("calories", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("protein_g", sa.Numeric(9, 1), nullable=False, server_default="0"),
            sa.Column("fat_g", sa.Numeric(9, 1), nullable=False, server_default="0"),
            sa.Column("carbs_g", sa.Numeric(9, 1), nullable=False, server_default="0"),
            sa.Column("plan_calories", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.UniqueConstraint("user_id", "date_utc", name="uq_daily_intake_user_date"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_daily_intake_user_id ON daily_intake (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_daily_intake_date_utc ON daily_intake (date_utc)")


def downgrade() -> None:
    # daily_intake
    op.drop_index("ix_daily_intake_date_utc", table_name="daily_intake")
    op.drop_index("ix_daily_intake_user_id", table_name="daily_intake")
    op.drop_table("daily_intake")

    # meal_photos
    op.drop_index("ix_meal_photos_meal_id", table_name="meal_photos")
    op.drop_table("meal_photos")

    # meal_items
    op.drop_index("ix_meal_items_meal_id", table_name="meal_items")
    op.drop_table("meal_items")

    # meals
    op.drop_index("ix_meals_consumed_at", table_name="meals")
    op.drop_index("ix_meals_user_id", table_name="meals")
    op.drop_table("meals")

    # users
    op.drop_column("users", "foodai_enabled_at")

    # Enums (safe drop)
    op.execute("DROP TYPE IF EXISTS meal_status")
    op.execute("DROP TYPE IF EXISTS meal_source")
