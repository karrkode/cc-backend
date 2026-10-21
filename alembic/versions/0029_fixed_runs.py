"""fixed runs: events on chosen dates only, with a time each (CC-96)

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-21 23:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'event_run_dates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('time', sa.String(5), nullable=False),
        sa.Column('end_time', sa.String(5), server_default='', nullable=False),
        sa.UniqueConstraint('event_id', 'date', name='uq_event_run_dates_day'),
    )
    op.create_index('ix_event_run_dates_event_id', 'event_run_dates', ['event_id'])
    op.create_index('ix_event_run_dates_date', 'event_run_dates', ['date'])


def downgrade() -> None:
    op.drop_table('event_run_dates')
