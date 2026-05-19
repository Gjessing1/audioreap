"""phase 11 staging path on acquisition jobs

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-05-19 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e1f2a3b4c5d6'
down_revision: str | None = 'd9e0f1a2b3c4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('acquisition_jobs', sa.Column('staging_path', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('acquisition_jobs', 'staging_path')
