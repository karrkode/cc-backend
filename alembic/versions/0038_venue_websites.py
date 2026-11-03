"""venues get an admin-only website, and dismissed website suggestions are remembered (CC-213)

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('website', sa.String(500), nullable=False, server_default=''))
    op.create_table(
        'venue_website_dismissals',
        sa.Column('venue_id', sa.Integer(), sa.ForeignKey('venues.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('url', sa.String(500), primary_key=True),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('venue_website_dismissals')
    op.drop_column('venues', 'website')
