"""session versions and each session's sign-in time (CC-203)

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-09 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0045'
down_revision = '0044'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('session_version', sa.Integer(), server_default='0', nullable=False))
    op.add_column('refresh_tokens', sa.Column('signed_in_at', sa.DateTime(timezone=True), nullable=True))
    # Sessions open at deploy keep going: their 90 days count from their latest refresh, as the first
    # sign-in time was never kept.
    op.execute("UPDATE refresh_tokens SET signed_in_at = created_at")
    with op.batch_alter_table('refresh_tokens') as b:
        b.alter_column('signed_in_at', nullable=False, server_default=sa.func.now())


def downgrade() -> None:
    with op.batch_alter_table('refresh_tokens') as b:
        b.drop_column('signed_in_at')
    with op.batch_alter_table('users') as b:
        b.drop_column('session_version')
