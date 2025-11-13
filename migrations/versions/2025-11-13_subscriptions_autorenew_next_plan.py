"""Add auto_renew and next_plan to subscriptions

Revision ID: subs20251113
Revises: subs20251111
Create Date: 2025-11-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "subs20251113"
down_revision: Union[str, None] = "subs20251111"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("subscriptions"):
        try:
            op.add_column(
                "subscriptions",
                sa.Column("auto_renew", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            )
        except Exception:
            pass
        try:
            op.add_column(
                "subscriptions",
                sa.Column("next_plan", sa.String(length=16), nullable=True),
            )
        except Exception:
            pass


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("subscriptions"):
        # Drop columns if they exist
        try:
            op.drop_column("subscriptions", "next_plan")
        except Exception:
            pass
        try:
            op.drop_column("subscriptions", "auto_renew")
        except Exception:
            pass
