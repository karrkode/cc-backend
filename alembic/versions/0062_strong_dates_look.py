"""CC-304: the stronger model's fallback for a show's exact dates: what it cost, and when it last looked.

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-20
"""
from alembic import op
import sqlalchemy as sa

revision = '0062'
down_revision = '0061'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Two new columns, one with a default and one nullable, so existing rows are untouched and the live site (which never
    # reads either) keeps working while this deploys. Nothing here can lose data; the downgrade drops what the upgrade added.
    op.add_column('discovery_checks', sa.Column('strong_usd', sa.Float(), nullable=False, server_default='0'))
    op.add_column('discovered_items', sa.Column('strong_looked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('discovered_items', 'strong_looked_at')
    op.drop_column('discovery_checks', 'strong_usd')
