"""add artist sort_name and acquisition_jobs retry_count

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-05-21 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'i5j6k7l8m9n0'
down_revision: str = 'h4i5j6k7l8m9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('artists', sa.Column('sort_name', sa.String(), nullable=True))
    op.add_column('acquisition_jobs', sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('acquisition_jobs', 'retry_count')
    op.drop_column('artists', 'sort_name')
