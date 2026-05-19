"""phase 7 tag quality scoring

Revision ID: d9e0f1a2b3c4
Revises: c7d8e9f0a1b2
Create Date: 2026-05-19 01:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd9e0f1a2b3c4'
down_revision: str | None = 'c7d8e9f0a1b2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('tracks', sa.Column('tag_quality_score', sa.Float(), nullable=True))
    op.add_column('track_files', sa.Column('has_cover_art', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('track_files', 'has_cover_art')
    op.drop_column('tracks', 'tag_quality_score')
