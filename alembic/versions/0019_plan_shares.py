"""plans shared with crews (CC-75)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-21 20:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'plan_shares',
        sa.Column('user_id', sa.Uuid(), primary_key=True),
        sa.Column('event_id', sa.Integer(), primary_key=True),
        sa.Column('crew_id', sa.Integer(), sa.ForeignKey('crews.id', ondelete='CASCADE'), primary_key=True),
        sa.ForeignKeyConstraint(['user_id', 'event_id'], ['plans.user_id', 'plans.event_id'], ondelete='CASCADE'),
    )
    op.create_index('ix_plan_shares_event_id', 'plan_shares', ['event_id'])
    op.create_index('ix_plan_shares_crew_id', 'plan_shares', ['crew_id'])


def downgrade() -> None:
    op.drop_table('plan_shares')
