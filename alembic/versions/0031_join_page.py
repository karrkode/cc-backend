"""join page: visits and sign-ups by source (CC-121)

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-26 17:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'traffic_join_visits',
        sa.Column('day', sa.Date(), primary_key=True),
        sa.Column('visitor', sa.String(40), primary_key=True),
        sa.Column('source', sa.String(60), primary_key=True, server_default=''),
        sa.Column('medium', sa.String(40), primary_key=True, server_default=''),
        sa.Column('campaign', sa.String(60), primary_key=True, server_default=''),
    )
    op.create_table(
        'traffic_join_signups',
        sa.Column('day', sa.Date(), primary_key=True),
        sa.Column('source', sa.String(60), primary_key=True, server_default=''),
        sa.Column('medium', sa.String(40), primary_key=True, server_default=''),
        sa.Column('campaign', sa.String(60), primary_key=True, server_default=''),
        sa.Column('signups', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_table('traffic_join_signups')
    op.drop_table('traffic_join_visits')
