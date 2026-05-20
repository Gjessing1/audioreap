"""merge file_mtime and album_jobs branches into main chain

Revision ID: g3h4i5j6k7l8
Revises: 4ce239bbbb58, 22fd0aa2df5b, e1f2a3b4c5d6
Create Date: 2026-05-20 00:00:00.000000

"""
from collections.abc import Sequence

revision: str = 'g3h4i5j6k7l8'
down_revision: tuple[str, ...] = ('4ce239bbbb58', '22fd0aa2df5b', 'e1f2a3b4c5d6')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
