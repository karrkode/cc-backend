"""culture crews: crews, their members, and invite links (CC-74)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-21 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'crews',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(60), nullable=False),
        sa.Column('description', sa.String(200), nullable=False, server_default=''),
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_crews_owner_id', 'crews', ['owner_id'])
    op.create_table(
        'crew_members',
        sa.Column('crew_id', sa.Integer(), sa.ForeignKey('crews.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('joined_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_crew_members_user_id', 'crew_members', ['user_id'])
    op.create_table(
        'crew_invites',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('crew_id', sa.Integer(), sa.ForeignKey('crews.id', ondelete='CASCADE'), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('created_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('uses', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_index('ix_crew_invites_crew_id', 'crew_invites', ['crew_id'])


def downgrade() -> None:
    op.drop_table('crew_invites')
    op.drop_table('crew_members')
    op.drop_table('crews')
