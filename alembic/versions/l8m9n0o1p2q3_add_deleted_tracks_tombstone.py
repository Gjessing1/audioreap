"""Add deleted_tracks tombstone table.

Revision ID: l8m9n0o1p2q3
Revises: k7l8m9n0o1p2
Create Date: 2026-05-23
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "l8m9n0o1p2q3"
down_revision = "k7l8m9n0o1p2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deleted_tracks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mb_recording_id", sa.String(), nullable=True),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.Column("track_title", sa.String(), nullable=True),
        sa.Column("track_artist", sa.String(), nullable=True),
        sa.Column("prevent_reimport", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_deleted_tracks_mb_recording_id", "deleted_tracks", ["mb_recording_id"])


def downgrade() -> None:
    op.drop_index("ix_deleted_tracks_mb_recording_id", table_name="deleted_tracks")
    op.drop_table("deleted_tracks")
