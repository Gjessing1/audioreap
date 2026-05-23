"""Add import_sessions table and provenance fields on acquisition_jobs.

Revision ID: m9n0o1p2q3r4
Revises: l8m9n0o1p2q3
Create Date: 2026-05-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "m9n0o1p2q3r4"
down_revision = "l8m9n0o1p2q3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_type", sa.String(), nullable=False),
        sa.Column("user_intent", sa.String(), nullable=True),
        sa.Column("strict_album_mode", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_release_group", sa.String(), nullable=True),
        sa.Column("target_release", sa.String(), nullable=True),
        sa.Column("source_playlist_id", sa.String(), nullable=True),
        sa.Column("album_job_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("artist", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    with op.batch_alter_table("acquisition_jobs") as batch_op:
        batch_op.add_column(sa.Column("import_session_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("acquired_from_release_group", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("acquired_from_release", sa.String(), nullable=True))
        batch_op.create_index("ix_acquisition_jobs_import_session_id", ["import_session_id"])


def downgrade() -> None:
    with op.batch_alter_table("acquisition_jobs") as batch_op:
        batch_op.drop_index("ix_acquisition_jobs_import_session_id")
        batch_op.drop_column("acquired_from_release")
        batch_op.drop_column("acquired_from_release_group")
        batch_op.drop_column("import_session_id")

    op.drop_table("import_sessions")
