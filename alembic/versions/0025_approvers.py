"""approvers of "I've been" photos (CC-91)

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-21 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('approver', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('users', sa.Column('approver_offered_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'approver_offered_at')
    op.drop_column('users', 'approver')
