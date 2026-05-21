"""Migrate staged state to needs_review; staged is no longer used.

Revision ID: j6k7l8m9n0o1
Revises: i5j6k7l8m9n0
Create Date: 2026-05-21 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

revision: str = "j6k7l8m9n0o1"
down_revision: str | None = "i5j6k7l8m9n0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE acquisition_jobs SET state = 'needs_review' WHERE state = 'staged'"
    )


def downgrade() -> None:
    pass  # cannot distinguish which rows were originally staged vs needs_review
