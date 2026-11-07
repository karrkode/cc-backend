"""record who a crew leader removed, so they stay out until invited back (CC-187)

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-06 21:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'crew_removals',
        sa.Column('crew_id', sa.Integer(), sa.ForeignKey('crews.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('removed_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('removed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_crew_removals_user_id', 'crew_removals', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_crew_removals_user_id', table_name='crew_removals')
    op.drop_table('crew_removals')
