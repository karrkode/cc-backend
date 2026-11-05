"""every venue with a website gets a discovery source, unless an admin deleted it (CC-221)

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-05 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0042'
down_revision = '0041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('venues', sa.Column('no_source', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('discovery_sources', sa.Column('added_from', sa.String(500), nullable=False, server_default=''))
    op.add_column('discovery_sources', sa.Column('find_link', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('discovery_sources', 'find_link')
    op.drop_column('discovery_sources', 'added_from')
    op.drop_column('venues', 'no_source')
