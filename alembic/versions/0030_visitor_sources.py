"""visitor sources: tagged links and referring sites on the Stats page (CC-110)

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-22 19:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'traffic_source_visits',
        sa.Column('day', sa.Date(), primary_key=True),
        sa.Column('visitor', sa.String(40), primary_key=True),
        sa.Column('source', sa.String(60), primary_key=True),
        sa.Column('medium', sa.String(40), primary_key=True, server_default=''),
        sa.Column('campaign', sa.String(60), primary_key=True, server_default=''),
    )
    op.create_table(
        'traffic_source_counts',
        sa.Column('day', sa.Date(), primary_key=True),
        sa.Column('source', sa.String(60), primary_key=True),
        sa.Column('medium', sa.String(40), primary_key=True, server_default=''),
        sa.Column('campaign', sa.String(60), primary_key=True, server_default=''),
        sa.Column('tickets', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_table('traffic_source_counts')
    op.drop_table('traffic_source_visits')
