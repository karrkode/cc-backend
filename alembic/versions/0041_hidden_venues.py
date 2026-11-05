"""an admin can hide a venue from the public venue list; every venue starts visible (CC-219)

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-05 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0041'
down_revision = '0040'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('hidden', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('venues', 'hidden')
