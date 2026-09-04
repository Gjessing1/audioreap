"""Add push_devices — credentials for the Android app's background check

Revision ID: r4s5t6u7v8w9
Revises: q3r4s5t6u7v8
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'r4s5t6u7v8w9'
down_revision = 'q3r4s5t6u7v8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'push_devices',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('last_event_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Unique: the hash is the lookup key for every poll, and two devices sharing one
    # would share a cursor.
    op.create_index(
        'ix_push_devices_token_hash', 'push_devices', ['token_hash'], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_push_devices_token_hash', table_name='push_devices')
    op.drop_table('push_devices')
