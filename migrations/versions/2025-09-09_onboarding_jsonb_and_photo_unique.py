"""Onboarding JSON->JSONB + meal_photos unique(tg_file_unique_id per meal)

Revision ID: a1b2c3d4e5f7
Revises: f0oda1f00d01
Create Date: 2025-09-09 12:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, None] = "f0oda1f00d01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Convert onboarding_answers.data and daily_plan to JSONB
    # Use explicit USING cast to avoid table rewrite errors
    op.execute(
        """
        ALTER TABLE onboarding_answers
        ALTER COLUMN data TYPE jsonb USING data::jsonb;
        """
    )
    op.execute(
        """
        ALTER TABLE onboarding_answers
        ALTER COLUMN daily_plan TYPE jsonb USING daily_plan::jsonb;
        """
    )

    # 2) Indexes for better filtering/analytics
    # GIN on data (jsonb_path_ops is compact and fast for containment)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_onboarding_answers_data_gin
        ON onboarding_answers USING gin (data jsonb_path_ops);
        """
    )
    # BTree on data->>'goal'
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_onb_ans_goal
        ON onboarding_answers ((data->>'goal'));
        """
    )
    # BTree on (daily_plan->>'calories')::int
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_onb_ans_plan_cal
        ON onboarding_answers (((daily_plan->>'calories')::int));
        """
    )


def downgrade() -> None:
    # Drop indexes
    op.execute("DROP INDEX IF EXISTS ix_onb_ans_plan_cal;")
    op.execute("DROP INDEX IF EXISTS ix_onb_ans_goal;")
    op.execute("DROP INDEX IF EXISTS ix_onboarding_answers_data_gin;")

    # Convert JSONB back to JSON
    op.execute(
        """
        ALTER TABLE onboarding_answers
        ALTER COLUMN daily_plan TYPE json USING daily_plan::json;
        """
    )
    op.execute(
        """
        ALTER TABLE onboarding_answers
        ALTER COLUMN data TYPE json USING data::json;
        """
    )
