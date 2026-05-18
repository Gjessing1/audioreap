"""phase 2.5 album jobs

Revision ID: 22fd0aa2df5b
Revises: 51a9732f1a50
Create Date: 2026-05-18 17:46:31.252514

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '22fd0aa2df5b'
down_revision: str | None = '51a9732f1a50'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'album_acquisition_jobs' not in existing_tables:
        op.create_table('album_acquisition_jobs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('album_ref', sa.String(), nullable=False),
        sa.Column('album_title', sa.String(), nullable=True),
        sa.Column('album_artist', sa.String(), nullable=True),
        sa.Column('track_count', sa.Integer(), nullable=True),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('policy', sa.String(), nullable=False),
        sa.Column('query', sa.String(), nullable=True),
        sa.Column('candidate_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )

    existing_cols = {c['name'] for c in inspector.get_columns('acquisition_jobs')}
    if 'album_job_id' not in existing_cols:
        op.add_column('acquisition_jobs', sa.Column('album_job_id', sa.String(), nullable=True))
    if 'track_index' not in existing_cols:
        op.add_column('acquisition_jobs', sa.Column('track_index', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('acquisition_jobs') as batch_op:
        batch_op.drop_column('track_index')
        batch_op.drop_column('album_job_id')
    op.drop_table('album_acquisition_jobs')
