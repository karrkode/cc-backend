"""cookie-free traffic counts

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'traffic_counts',
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=20), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('day', 'kind', 'name'),
    )
    op.create_table(
        'traffic_visitors',
        sa.Column('visitor', sa.String(length=40), nullable=False),
        sa.Column('first_day', sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint('visitor'),
    )
    op.create_table(
        'traffic_visitor_days',
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('visitor', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('day', 'visitor'),
    )


def downgrade():
    op.drop_table('traffic_visitor_days')
    op.drop_table('traffic_visitors')
    op.drop_table('traffic_counts')
