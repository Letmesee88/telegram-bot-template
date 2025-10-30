"""User weight logs table

Revision ID: weightlog20251030
Revises: tpl0tz20251006
Create Date: 2025-10-30 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "weightlog20251030"
down_revision: Union[str, None] = "tpl0tz20251006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("weight_logs"):
        op.create_table(
            "weight_logs",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("weight_kg", sa.Numeric(5, 1), nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.Column("recorded_local_date", sa.Date(), nullable=False),
            sa.Column("source", sa.String(length=32), server_default=sa.text("'manual'"), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
            sa.UniqueConstraint("user_id", "recorded_local_date", name="uq_weight_user_local_date"),
        )
    # Indexes
    op.execute("CREATE INDEX IF NOT EXISTS ix_weight_logs_user_id ON weight_logs (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_weight_logs_recorded_at ON weight_logs (recorded_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_weight_logs_recorded_local_date ON weight_logs (recorded_local_date)")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("weight_logs"):
        op.execute("DROP INDEX IF EXISTS ix_weight_logs_recorded_local_date")
        op.execute("DROP INDEX IF EXISTS ix_weight_logs_recorded_at")
        op.execute("DROP INDEX IF EXISTS ix_weight_logs_user_id")
        op.drop_table("weight_logs")
