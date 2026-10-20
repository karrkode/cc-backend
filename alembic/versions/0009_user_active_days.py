"""days each member was active, for the admin Stats page

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_active_days',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'day'),
    )
    op.create_index(op.f('ix_user_active_days_day'), 'user_active_days', ['day'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_user_active_days_day'), table_name='user_active_days')
    op.drop_table('user_active_days')
