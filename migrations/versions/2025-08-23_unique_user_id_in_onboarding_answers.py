"""Make user_id unique in onboarding_answers"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b7f3e2c9d1a0"
down_revision: Union[str, None] = "a1b2c3d4e5f6"  # 2025-08-22_onboarding_answers.py
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Drop non-unique index if exists
    op.drop_index("ix_onboarding_answers_user_id", table_name="onboarding_answers")

    # 2) Remove duplicates keeping the newest per user_id (Postgres)
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY user_id
                       ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
                   ) AS rn
            FROM onboarding_answers
        )
        DELETE FROM onboarding_answers oa
        USING ranked r
        WHERE oa.id = r.id
          AND r.rn > 1;
        """
    )

    # 3) Add unique constraint
    op.create_unique_constraint(
        "uq_onboarding_answers_user_id",
        "onboarding_answers",
        ["user_id"],
    )


def downgrade() -> None:
    # Drop unique constraint
    op.drop_constraint(
        "uq_onboarding_answers_user_id",
        "onboarding_answers",
        type_="unique",
    )
    # Recreate non-unique index
    op.create_index(
        "ix_onboarding_answers_user_id",
        "onboarding_answers",
        ["user_id"],
        unique=False
    )