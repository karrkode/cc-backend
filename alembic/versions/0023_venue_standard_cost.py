"""a venue's standard cost, for its events that don't post a price (CC-89)

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-21 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('standard_cost', sa.String(120), nullable=False, server_default=''))
    op.add_column('venues', sa.Column('standard_min_price', sa.Numeric(8, 2), nullable=True))
    op.create_check_constraint('ck_venues_standard_min_price_not_negative', 'venues',
                               'standard_min_price IS NULL OR standard_min_price >= 0')


def downgrade() -> None:
    op.drop_constraint('ck_venues_standard_min_price_not_negative', 'venues', type_='check')
    op.drop_column('venues', 'standard_min_price')
    op.drop_column('venues', 'standard_cost')
