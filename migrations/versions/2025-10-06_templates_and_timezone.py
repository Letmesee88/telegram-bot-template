"""Meal templates feature and per-user timezone

Revision ID: tpl0tz20251006
Revises: rec0mmend0log01
Create Date: 2025-10-06 16:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "tpl0tz20251006"
down_revision: Union[str, None] = "rec0mmend0log01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


template_category = postgresql.ENUM(
    "breakfast", "lunch", "dinner", "snack", name="template_category", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # users.timezone (nullable)
    user_columns = {c["name"] for c in inspector.get_columns("users")}
    if "timezone" not in user_columns:
        op.add_column("users", sa.Column("timezone", sa.String(length=64), nullable=True))

    # Ensure template_category enum exists
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'template_category') THEN
                CREATE TYPE template_category AS ENUM ('breakfast', 'lunch', 'dinner', 'snack');
            END IF;
        END$$;
        """
    )

    # meal_templates
    if not inspector.has_table("meal_templates"):
        op.create_table(
            "meal_templates",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("category", template_category, nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("calories", sa.Integer(), nullable=True),
            sa.Column("protein_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("fat_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("carbs_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("weight_g", sa.Numeric(8, 1), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("TIMEZONE('utc', now())"),
                nullable=True,
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_meal_templates_user_id ON meal_templates (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_meal_templates_category ON meal_templates (category)")

    # Functional unique index: (user_id, category, lower(trim(title)))
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = 'uq_tpl_user_cat_title_norm'
            ) THEN
                CREATE UNIQUE INDEX uq_tpl_user_cat_title_norm
                ON meal_templates (user_id, category, lower(btrim(title)));
            END IF;
        END$$;
        """
    )

    # meal_template_items
    if not inspector.has_table("meal_template_items"):
        op.create_table(
            "meal_template_items",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column(
                "template_id",
                sa.Integer(),
                sa.ForeignKey("meal_templates.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("weight_g", sa.Numeric(8, 1), nullable=True),
            sa.Column("calories", sa.Numeric(8, 1), nullable=True),
            sa.Column("protein_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("fat_g", sa.Numeric(7, 1), nullable=True),
            sa.Column("carbs_g", sa.Numeric(7, 1), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_meal_template_items_template_id ON meal_template_items (template_id)")

    # Extend meal_source enum with 'template' if not present
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_enum e
                JOIN pg_type t ON e.enumtypid = t.oid
                WHERE t.typname = 'meal_source' AND e.enumlabel = 'template'
            ) THEN
                ALTER TYPE meal_source ADD VALUE 'template';
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    # Best-effort downgrade: drop new tables and indexes; keep enum value
    if op.get_bind().dialect.has_table(op.get_bind(), "meal_template_items"):
        op.drop_index("ix_meal_template_items_template_id", table_name="meal_template_items")
        op.drop_table("meal_template_items")
    if op.get_bind().dialect.has_table(op.get_bind(), "meal_templates"):
        op.execute("DROP INDEX IF EXISTS uq_tpl_user_cat_title_norm")
        op.drop_index("ix_meal_templates_category", table_name="meal_templates")
        op.drop_index("ix_meal_templates_user_id", table_name="meal_templates")
        op.drop_table("meal_templates")

    # users.timezone
    try:
        op.drop_column("users", "timezone")
    except Exception:
        pass

    # Do not drop enum template_category (could be referenced); leaving as-is.
