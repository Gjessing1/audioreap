"""Add quality_suppressed to tracks table."""
from alembic import op
import sqlalchemy as sa

revision = 'o1p2q3r4s5t6'
down_revision = 'n0o1p2q3r4s5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tracks', sa.Column('quality_suppressed', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('tracks', 'quality_suppressed')
