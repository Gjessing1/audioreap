"""phase 13 metadata review gate

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-20 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'f2a3b4c5d6e7'
down_revision: str | None = 'g3h4i5j6k7l8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'acquisition_jobs',
        sa.Column('resolved_metadata_json', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('acquisition_jobs', 'resolved_metadata_json')
