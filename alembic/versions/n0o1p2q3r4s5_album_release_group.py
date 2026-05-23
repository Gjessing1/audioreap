"""Add mb_release_group_id to albums table."""
from alembic import op
import sqlalchemy as sa

revision = 'n0o1p2q3r4s5'
down_revision = 'm9n0o1p2q3r4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('albums', sa.Column('mb_release_group_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('albums', 'mb_release_group_id')
