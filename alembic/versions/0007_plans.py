"""plans: interested, or going on a date

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 16:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

plan_status = sa.Enum('interested', 'going', name='plan_status')


def upgrade():
    op.create_table(
        'plans',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('status', plan_status, nullable=False),
        sa.Column('going_on', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("(status = 'going') = (going_on IS NOT NULL)", name='ck_plans_going_has_a_date'),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'event_id'),
    )
    op.create_index('ix_plans_user_status', 'plans', ['user_id', 'status'], unique=False)


def downgrade():
    op.drop_index('ix_plans_user_status', table_name='plans')
    op.drop_table('plans')
    plan_status.drop(op.get_bind(), checkfirst=True)
