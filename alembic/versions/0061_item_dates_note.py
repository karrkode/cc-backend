"""CC-302: a note on each found event about its look for exact dates on a tickets or calendar page.

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

revision = '0061'
down_revision = '0060'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One new column with a default, so every existing row gets '' and the live site (which never reads it) is unaffected.
    op.add_column('discovered_items', sa.Column('dates_note', sa.String(1000), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('discovered_items', 'dates_note')
