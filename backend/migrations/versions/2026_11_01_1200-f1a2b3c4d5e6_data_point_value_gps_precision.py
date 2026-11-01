"""data_point_series.value with 6 decimals (GPS coordinates)

Latitude and longitude samples (FIT files, the mobile SDK workout route) were rounded
to 3 decimals by NUMERIC(10, 3): about 110 m, useless for a route map. NUMERIC(15, 6)
keeps ~0.1 m and still holds every value the old column could (9 integer digits).

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "data_point_series",
        "value",
        type_=sa.Numeric(15, 6),
        existing_type=sa.Numeric(10, 3),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "data_point_series",
        "value",
        type_=sa.Numeric(10, 3),
        existing_type=sa.Numeric(15, 6),
        existing_nullable=False,
    )
