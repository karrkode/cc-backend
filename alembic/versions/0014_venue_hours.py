"""venue opening hours and usual show times (CC-78)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('open_hours', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('venues', sa.Column('open_hours_by', sa.String(20), nullable=False, server_default=''))
    op.add_column('venues', sa.Column('show_times', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('venues', sa.Column('show_times_by', sa.String(20), nullable=False, server_default=''))


def downgrade() -> None:
    for col in ('show_times_by', 'show_times', 'open_hours_by', 'open_hours'):
        op.drop_column('venues', col)
