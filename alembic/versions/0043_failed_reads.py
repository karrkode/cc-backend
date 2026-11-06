"""count a source's failed reads in a row, so it can wait longer after each (CC-198)

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-06 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('discovery_sources', sa.Column('failed_reads', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('discovery_sources', 'failed_reads')
