"""Recommendations log table for anti-repeat persistence

Revision ID: rec0mmend0log01
Revises: a1b2c3d4e5f7
Create Date: 2025-10-02 13:18:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "rec0mmend0log01"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("recommendations_log"):
        op.create_table(
            "recommendations_log",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column(
                "ts",
                sa.DateTime(timezone=True),
                server_default=sa.text("TIMEZONE('utc', now())"),
                nullable=False,
            ),
        )
    # Indexes (idempotent)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_recommendations_log_user_id ON recommendations_log (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_recommendations_log_user_ts ON recommendations_log (user_id, ts)"
    )


def downgrade() -> None:
    op.drop_index("ix_recommendations_log_user_ts", table_name="recommendations_log")
    op.drop_index("ix_recommendations_log_user_id", table_name="recommendations_log")
    op.drop_table("recommendations_log")
