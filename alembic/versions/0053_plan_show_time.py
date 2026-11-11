"""a going plan's show time (CC-264)

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-11 12:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0053'
down_revision = '0052'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CC-264: which show you're going to on a day with more than one ("16:00"); null when the day lists no time,
    # and for every plan made before this (the calendar file then takes the day's first show, as before)
    with op.batch_alter_table('plans') as b:
        b.add_column(sa.Column('going_at', sa.String(5), nullable=True))
        b.create_check_constraint('ck_plans_time_only_when_going', "going_at IS NULL OR status = 'going'")


def downgrade() -> None:
    with op.batch_alter_table('plans') as b:
        b.drop_constraint('ck_plans_time_only_when_going', type_='check')
        b.drop_column('going_at')
