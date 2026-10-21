"""which crews see an "I've been" mark (CC-93)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-21 20:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'attended_shares',
        sa.Column('user_id', sa.Uuid(), primary_key=True),
        sa.Column('event_id', sa.Integer(), primary_key=True),
        sa.Column('crew_id', sa.Integer(), sa.ForeignKey('crews.id', ondelete='CASCADE'), primary_key=True),
        sa.ForeignKeyConstraint(['user_id', 'event_id'], ['attended.user_id', 'attended.event_id'], ondelete='CASCADE'),
    )
    op.create_index('ix_attended_shares_event_id', 'attended_shares', ['event_id'])
    op.create_index('ix_attended_shares_crew_id', 'attended_shares', ['crew_id'])


def downgrade() -> None:
    op.drop_table('attended_shares')
