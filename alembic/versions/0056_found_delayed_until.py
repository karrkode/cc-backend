"""CC-257: a found event an admin delayed comes back to the queue on its own.

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0056'
down_revision = '0055'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, so existing rows are untouched; the API (culture_app) can't add columns, so it has to be a migration.
    with op.batch_alter_table('discovered_items') as b:
        b.add_column(sa.Column('delayed_until', sa.DateTime(timezone=True), nullable=True))
        b.create_index('ix_discovered_items_delayed_until', ['delayed_until'])


def downgrade() -> None:
    with op.batch_alter_table('discovered_items') as b:
        b.drop_index('ix_discovered_items_delayed_until')
        b.drop_column('delayed_until')
