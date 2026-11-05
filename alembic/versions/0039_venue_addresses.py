"""venues get a public address (street, city and ZIP), and discovery keeps suggestions for an admin to confirm (CC-218)

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0039'
down_revision = '0038'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('address', sa.String(200), nullable=False, server_default=''))
    op.add_column('venues', sa.Column('city', sa.String(120), nullable=False, server_default=''))
    op.add_column('venues', sa.Column('zip_code', sa.String(10), nullable=False, server_default=''))
    op.create_table(
        'venue_address_suggestions',
        sa.Column('venue_id', sa.Integer(), sa.ForeignKey('venues.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('address', sa.String(200), primary_key=True),
        sa.Column('city', sa.String(120), nullable=False, server_default=''),
        sa.Column('zip_code', sa.String(10), nullable=False, server_default=''),
        sa.Column('source', sa.String(200), nullable=False, server_default=''),
        sa.Column('seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('venue_address_suggestions')
    op.drop_column('venues', 'zip_code')
    op.drop_column('venues', 'city')
    op.drop_column('venues', 'address')
