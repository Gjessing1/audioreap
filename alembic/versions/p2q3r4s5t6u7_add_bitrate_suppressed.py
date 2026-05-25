"""Add bitrate_suppressed column to tracks

Revision ID: p2q3r4s5t6u7
Revises: o1p2q3r4s5t6
Create Date: 2026-05-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'p2q3r4s5t6u7'
down_revision = 'o1p2q3r4s5t6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tracks', sa.Column('bitrate_suppressed', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('tracks', 'bitrate_suppressed')
