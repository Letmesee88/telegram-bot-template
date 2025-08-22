"""onboarding answers

Revision ID: a1b2c3d4e5f6
Revises: e4b7e8c165c1
Create Date: 2025-08-22 18:53:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e4b7e8c165c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "onboarding_answers",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("daily_plan", sa.JSON(), nullable=False),
        sa.Column("goal", sa.String(length=16), nullable=True),
        sa.Column("calories", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("TIMEZONE('utc', now())"),
            onupdate=sa.text("TIMEZONE('utc', now())"),
            nullable=True,
        ),
    )
    op.create_index("ix_onboarding_answers_user_id", "onboarding_answers", ["user_id"], unique=False)
    op.create_index("ix_onboarding_answers_goal", "onboarding_answers", ["goal"], unique=False)
    op.create_index("ix_onboarding_answers_calories", "onboarding_answers", ["calories"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_onboarding_answers_calories", table_name="onboarding_answers")
    op.drop_index("ix_onboarding_answers_goal", table_name="onboarding_answers")
    op.drop_index("ix_onboarding_answers_user_id", table_name="onboarding_answers")
    op.drop_table("onboarding_answers")
