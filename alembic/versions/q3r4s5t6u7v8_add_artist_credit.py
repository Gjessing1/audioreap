"""Add artist_credit column to tracks

Revision ID: q3r4s5t6u7v8
Revises: p2q3r4s5t6u7
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'q3r4s5t6u7v8'
down_revision = 'p2q3r4s5t6u7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tracks', sa.Column('artist_credit', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('tracks', 'artist_credit')
