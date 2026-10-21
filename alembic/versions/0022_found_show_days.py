"""show days: found events' show days and weeks of the month, events' weeks of the month, and when found
events' pages were read for what's missing (CC-88)

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-21 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('discovered_items', sa.Column('weekly', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('discovered_items', sa.Column('looked_on', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('discovered_items', sa.Column('weeks', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('events', sa.Column('weeks', sa.JSON(), nullable=False, server_default='{}'))


def downgrade() -> None:
    op.drop_column('events', 'weeks')
    op.drop_column('discovered_items', 'weeks')
    op.drop_column('discovered_items', 'looked_on')
    op.drop_column('discovered_items', 'weekly')
