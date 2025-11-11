"""Subscriptions and Payments tables

Revision ID: subs20251111
Revises: drep0rt20251110
Create Date: 2025-11-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "subs20251111"
down_revision: Union[str, None] = "drep0rt20251110"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

subscription_status_enum = postgresql.ENUM(
    "active", "past_due", "canceled", name="subscription_status", create_type=False
)
subscription_plan_enum = postgresql.ENUM(
    "trial", "month", "year", name="subscription_plan", create_type=False
)
payment_status_enum = postgresql.ENUM(
    "pending", "succeeded", "canceled", "waiting_for_capture", name="payment_status", create_type=False
)

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Create enums if not exist
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'subscription_status') THEN
                CREATE TYPE subscription_status AS ENUM ('active','past_due','canceled');
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'subscription_plan') THEN
                CREATE TYPE subscription_plan AS ENUM ('trial','month','year');
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payment_status') THEN
                CREATE TYPE payment_status AS ENUM ('pending','succeeded','canceled','waiting_for_capture');
            END IF;
        END$$;
        """
    )

    if not inspector.has_table("subscriptions"):
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("status", subscription_status_enum, nullable=False, server_default="active"),
            sa.Column("plan", subscription_plan_enum, nullable=False, server_default="month"),
            sa.Column("payment_method_id", sa.String(length=128), nullable=True),
            sa.Column("started_at_utc", sa.DateTime(timezone=True), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.Column("expires_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("canceled_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
        )
        op.execute("CREATE INDEX IF NOT EXISTS ix_subscriptions_user_id ON subscriptions (user_id)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON subscriptions (status)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_subscriptions_plan ON subscriptions (plan)")

    if not inspector.has_table("payments"):
        op.create_table(
            "payments",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=True),
            sa.Column("yk_payment_id", sa.String(length=64), nullable=False, unique=True),
            sa.Column("idempotence_key", sa.String(length=64), nullable=True),
            sa.Column("payment_method_id", sa.String(length=128), nullable=True),
            sa.Column("amount_value", sa.Numeric(10, 2), nullable=False),
            sa.Column("currency", sa.String(length=3), server_default="RUB", nullable=False),
            sa.Column("status", payment_status_enum, nullable=False, server_default="pending"),
            sa.Column("description", sa.String(length=128), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=True),
        )
        op.execute("CREATE INDEX IF NOT EXISTS ix_payments_user_id ON payments (user_id)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_payments_subscription_id ON payments (subscription_id)")
        op.execute("CREATE INDEX IF NOT EXISTS ix_payments_status ON payments (status)")
        op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_yk_payment_id ON payments (yk_payment_id)")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("payments"):
        op.drop_index("ix_payments_yk_payment_id", table_name="payments")
        op.drop_index("ix_payments_status", table_name="payments")
        op.drop_index("ix_payments_subscription_id", table_name="payments")
        op.drop_index("ix_payments_user_id", table_name="payments")
        op.drop_table("payments")

    if inspector.has_table("subscriptions"):
        op.drop_index("ix_subscriptions_plan", table_name="subscriptions")
        op.drop_index("ix_subscriptions_status", table_name="subscriptions")
        op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
        op.drop_table("subscriptions")

    # Keep enum types for safety; do not drop to avoid breaking historical rows.
