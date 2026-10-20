"""crew invites by email address, shown on sign-in (CC-59)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-21 23:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'crew_email_invites',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('crew_id', sa.Integer(), sa.ForeignKey('crews.id', ondelete='CASCADE'), nullable=False),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('invited_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('crew_id', 'email', name='uq_crew_email_invite'),
    )
    op.create_index('ix_crew_email_invites_crew_id', 'crew_email_invites', ['crew_id'])
    op.create_index('ix_crew_email_invites_email', 'crew_email_invites', ['email'])


def downgrade() -> None:
    op.drop_table('crew_email_invites')
