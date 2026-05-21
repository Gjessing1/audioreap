"""add genre column to tracks and performance indexes

Revision ID: h4i5j6k7l8m9
Revises: f2a3b4c5d6e7
Create Date: 2026-05-21 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'h4i5j6k7l8m9'
down_revision: str = 'f2a3b4c5d6e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tracks', sa.Column('genre', sa.String(), nullable=True))
    op.create_index('ix_acquisition_jobs_state', 'acquisition_jobs', ['state'])
    op.create_index('ix_tracks_mb_recording_id', 'tracks', ['musicbrainz_recording_id'])


def downgrade() -> None:
    op.drop_index('ix_tracks_mb_recording_id', table_name='tracks')
    op.drop_index('ix_acquisition_jobs_state', table_name='acquisition_jobs')
    op.drop_column('tracks', 'genre')
