"""Daily reports log table

Revision ID: drep0rt20251110
Revises: weightlog20251030
Create Date: 2025-11-10 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "drep0rt20251110"
down_revision: Union[str, None] = "weightlog20251030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

status_enum = postgresql.ENUM(
    "queued", "sent", "failed", "skipped", name="daily_report_status", create_type=False
)

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'daily_report_status') THEN
                CREATE TYPE daily_report_status AS ENUM ('queued','sent','failed','skipped');
            END IF;
        END$$;
        """
    )

    if not inspector.has_table("daily_reports_log"):
        op.create_table(
            "daily_reports_log",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("date_local", sa.Date(), nullable=False),
            sa.Column("status", status_enum, nullable=False, server_default="queued"),
            sa.Column("message_id", sa.Integer(), nullable=True),
            sa.Column("sent_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_text", sa.String(length=512), nullable=True),
            sa.Column("retries", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.UniqueConstraint("user_id", "date_local", name="uq_daily_reports_user_date"),
        )
        op.create_index("ix_daily_reports_log_user_id", "daily_reports_log", ["user_id"], unique=False)
        op.create_index("ix_daily_reports_log_date", "daily_reports_log", ["date_local"], unique=False)
        op.create_index("ix_daily_reports_log_status", "daily_reports_log", ["status"], unique=False)


def downgrade() -> None:
    if op.get_bind().dialect.has_table(op.get_bind(), "daily_reports_log"):
        op.drop_index("ix_daily_reports_log_status", table_name="daily_reports_log")
        op.drop_index("ix_daily_reports_log_date", table_name="daily_reports_log")
        op.drop_index("ix_daily_reports_log_user_id", table_name="daily_reports_log")
        op.drop_table("daily_reports_log")
    # Keep enum type for safety (may be referenced by old rows); do not drop.
