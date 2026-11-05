"""venues added by members' submissions wait for review; existing venues stay approved (CC-188)

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-05 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0040'
down_revision = '0039'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('pending', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('venues', 'pending')
