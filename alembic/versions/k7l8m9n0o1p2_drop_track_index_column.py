"""Drop unused track_index column from acquisition_jobs

Revision ID: k7l8m9n0o1p2
Revises: j6k7l8m9n0o1
Create Date: 2026-05-23

track_index was written by album_pipeline but never read anywhere.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k7l8m9n0o1p2"
down_revision: str | None = "j6k7l8m9n0o1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("acquisition_jobs") as batch_op:
        batch_op.drop_column("track_index")


def downgrade() -> None:
    with op.batch_alter_table("acquisition_jobs") as batch_op:
        batch_op.add_column(sa.Column("track_index", sa.Integer(), nullable=True))
