"""menstrual cycle daily flow (per-day flow of cycles derived from SDK data)

Revision ID: e7f8a9b0c1d2
Revises: c4d5e6f7a8b9

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "menstrual_cycle_details",
        sa.Column("daily_flow", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("menstrual_cycle_details", "daily_flow")
