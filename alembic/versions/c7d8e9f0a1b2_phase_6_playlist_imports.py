"""phase 6 playlist imports

Revision ID: c7d8e9f0a1b2
Revises: 51a9732f1a50
Create Date: 2026-05-19 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c7d8e9f0a1b2'
down_revision: str | None = '51a9732f1a50'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'playlist_imports',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('track_count', sa.Integer(), nullable=True),
        sa.Column('enqueued_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('owned_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('state', sa.String(), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column('acquisition_jobs', sa.Column('playlist_import_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('acquisition_jobs', 'playlist_import_id')
    op.drop_table('playlist_imports')
