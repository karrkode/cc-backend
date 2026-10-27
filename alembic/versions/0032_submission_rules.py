"""auto-reject rules for member submissions (CC-100)

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-27 15:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'submission_rules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('phrase', sa.String(200), nullable=False, server_default=''),
        sa.Column('venue_id', sa.Integer(), sa.ForeignKey('venues.id', ondelete='CASCADE')),
        sa.Column('caught', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column('events', sa.Column('reject_reason', sa.String(200), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('events', 'reject_reason')
    op.drop_table('submission_rules')
