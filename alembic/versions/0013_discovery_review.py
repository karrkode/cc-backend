"""discovery review: skip rules; an event remembers the source it was found on

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'discovery_skip_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('phrase', sa.String(200), nullable=False),
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('discovery_sources.id', ondelete='CASCADE')),
        sa.Column('caught', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column('events', sa.Column('found_by_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_events_found_by_id', 'events', 'discovery_sources', ['found_by_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_events_found_by_id', 'events', type_='foreignkey')
    op.drop_column('events', 'found_by_id')
    op.drop_table('discovery_skip_rules')
