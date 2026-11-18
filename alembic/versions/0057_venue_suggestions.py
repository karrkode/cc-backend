"""CC-209: members suggest a venue's calendar link; admins accept or decline.

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa

revision = '0057'
down_revision = '0056'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new table, so existing rows are untouched; the API (culture_app) can't create tables, so it has to be a migration.
    op.create_table(
        'venue_suggestions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('url', sa.String(500), nullable=False),
        sa.Column('venue_name', sa.String(200), nullable=False, server_default=''),
        sa.Column('note', sa.String(500), nullable=False, server_default=''),
        sa.Column('status', sa.String(10), nullable=False, server_default='pending'),
        sa.Column('decline_reason', sa.String(200), nullable=False, server_default=''),
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('discovery_sources.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_venue_suggestions_user_id', 'venue_suggestions', ['user_id'])
    op.create_index('ix_venue_suggestions_status', 'venue_suggestions', ['status'])


def downgrade() -> None:
    op.drop_index('ix_venue_suggestions_status', table_name='venue_suggestions')
    op.drop_index('ix_venue_suggestions_user_id', table_name='venue_suggestions')
    op.drop_table('venue_suggestions')
