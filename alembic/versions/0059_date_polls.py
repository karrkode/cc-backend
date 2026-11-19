"""CC-297: crew date polls: four new tables.

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa

revision = '0059'
down_revision = '0058'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New tables only, so no existing row changes. The API (culture_app) can't create tables, so it has to be a migration.
    op.create_table(
        'date_polls',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('crew_id', sa.Integer(), sa.ForeignKey('crews.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id', ondelete='CASCADE'), nullable=False),
        sa.Column('started_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(10), nullable=False, server_default='shows'),
        sa.Column('status', sa.String(10), nullable=False, server_default='open'),
        sa.Column('window_from', sa.Date(), nullable=False),
        sa.Column('window_to', sa.Date(), nullable=False),
        sa.Column('locked_option_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_date_polls_crew_id', 'date_polls', ['crew_id'])
    op.create_index('ix_date_polls_event_id', 'date_polls', ['event_id'])
    # One open poll per crew per event; a locked or cancelled one doesn't block the next.
    op.create_index('uq_date_polls_one_open', 'date_polls', ['crew_id', 'event_id'], unique=True,
                    postgresql_where=sa.text("status = 'open'"))
    op.create_table(
        'date_poll_options',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('poll_id', sa.Integer(), sa.ForeignKey('date_polls.id', ondelete='CASCADE'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('time', sa.String(5), nullable=False, server_default=''),
        sa.UniqueConstraint('poll_id', 'date', 'time', name='uq_date_poll_options'),
    )
    op.create_index('ix_date_poll_options_poll_id', 'date_poll_options', ['poll_id'])
    op.create_table(
        'date_poll_answers',
        sa.Column('poll_id', sa.Integer(), sa.ForeignKey('date_polls.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('answered_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_date_poll_answers_user_id', 'date_poll_answers', ['user_id'])
    op.create_table(
        'date_poll_picks',
        sa.Column('option_id', sa.Integer(), sa.ForeignKey('date_poll_options.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    )
    op.create_index('ix_date_poll_picks_user_id', 'date_poll_picks', ['user_id'])


def downgrade() -> None:
    # Only poll data is lost.
    op.drop_table('date_poll_picks')
    op.drop_table('date_poll_answers')
    op.drop_table('date_poll_options')
    op.drop_table('date_polls')
