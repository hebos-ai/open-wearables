"""planned workout push (idempotent re-send and delete by external_id)

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a1

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b2c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "planned_workout_push",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("provider_plan_id", sa.Text(), nullable=True),
        sa.Column("provider_workout_id", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_planned_workout_push_user_provider_ext",
        "planned_workout_push",
        ["user_id", "provider", "external_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_planned_workout_push_user_provider_ext", table_name="planned_workout_push")
    op.drop_table("planned_workout_push")
