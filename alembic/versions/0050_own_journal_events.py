"""members' own journal events and categories, private to them (CC-248)

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-10 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0050'
down_revision = '0049'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'journal_categories',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(30), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('user_id', 'name', name='uq_journal_categories_name'),
    )
    op.create_index('ix_journal_categories_user_id', 'journal_categories', ['user_id'])
    op.create_table(
        'own_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(120), nullable=False),
        sa.Column('on_date', sa.Date(), nullable=False),
        sa.Column('venue', sa.String(120), server_default='', nullable=False),
        sa.Column('category', sa.String(40), nullable=True),
        sa.Column('category_id', sa.Integer(), sa.ForeignKey('journal_categories.id', ondelete='SET NULL'), nullable=True),
        sa.Column('words', sa.String(62), server_default='', nullable=False),
        sa.Column('review', sa.String(1000), server_default='', nullable=False),
        sa.Column('recommend', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('photo_key', sa.String(200), nullable=True, unique=True),
        sa.Column('photo_type', sa.String(40), server_default='', nullable=False),
        sa.Column('photo_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_own_events_user_id', 'own_events', ['user_id'])
    op.create_index('ix_own_events_category_id', 'own_events', ['category_id'])


def downgrade() -> None:
    op.drop_table('own_events')
    op.drop_table('journal_categories')
