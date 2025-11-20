"""Add users.email column for receipt customer contact

Revision ID: users_email_20251120
Revises: subs20251113
Create Date: 2025-11-20 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "users_email_20251120"
down_revision: Union[str, None] = "subs20251113"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("users"):
        cols = [c.get("name") for c in inspector.get_columns("users")]  # type: ignore[attr-defined]
        if "email" not in (cols or []):
            try:
                op.add_column("users", sa.Column("email", sa.String(length=255), nullable=True))
            except Exception:
                pass


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("users"):
        try:
            op.drop_column("users", "email")
        except Exception:
            pass
