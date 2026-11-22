"""CC-298: exhibition date polls: half-hour blocks, the day's hours, and the locked range.

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa

revision = '0063'
down_revision = '0062'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One new table and four nullable columns on CC-297's tables, so no existing row changes. The API (culture_app)
    # can't change the schema, so it has to be a migration.
    op.create_table(
        'date_poll_blocks',
        sa.Column('option_id', sa.Integer(), sa.ForeignKey('date_poll_options.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('start', sa.String(5), primary_key=True),
    )
    op.create_index('ix_date_poll_blocks_user_id', 'date_poll_blocks', ['user_id'])
    op.add_column('date_poll_options', sa.Column('opens', sa.String(5), nullable=True))
    op.add_column('date_poll_options', sa.Column('closes', sa.String(5), nullable=True))
    op.add_column('date_polls', sa.Column('locked_start', sa.String(5), nullable=True))
    op.add_column('date_polls', sa.Column('locked_end', sa.String(5), nullable=True))


def downgrade() -> None:
    # Only exhibition-poll data goes (CC-297's showtime polls are untouched).
    op.execute("DELETE FROM date_polls WHERE kind = 'hours'")
    op.drop_column('date_polls', 'locked_end')
    op.drop_column('date_polls', 'locked_start')
    op.drop_column('date_poll_options', 'closes')
    op.drop_column('date_poll_options', 'opens')
    op.drop_index('ix_date_poll_blocks_user_id', table_name='date_poll_blocks')
    op.drop_table('date_poll_blocks')
