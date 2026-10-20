"""discovery sources that are checked by hand only (CC-81)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-21 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('discovery_sources', sa.Column('manual', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('discovery_sources', 'manual')
