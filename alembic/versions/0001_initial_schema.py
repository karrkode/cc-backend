"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-16 20:15:35.530430
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=80), nullable=False),
    sa.Column('role', sa.Enum('user', 'admin', name='user_role'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('venues',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('neighborhood', sa.String(length=120), nullable=False),
    sa.Column('area', sa.Enum('city', 'suburbs', 'out_of_town', name='venue_area'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', 'neighborhood', name='uq_venues_name_neighborhood')
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_id', sa.String(length=40), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('venue_id', sa.Integer(), nullable=False),
    sa.Column('begin_date', sa.Date(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('open_run', sa.Boolean(), nullable=False),
    sa.Column('cost_text', sa.String(length=120), nullable=False),
    sa.Column('min_price', sa.Numeric(precision=8, scale=2), nullable=True),
    sa.Column('category', sa.String(length=40), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'approved', 'rejected', name='event_status'), nullable=False),
    sa.Column('submitted_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('end_date IS NULL OR begin_date IS NULL OR end_date >= begin_date', name='ck_events_dates_in_order'),
    sa.CheckConstraint('min_price IS NULL OR min_price >= 0', name='ck_events_min_price_not_negative'),
    sa.ForeignKeyConstraint(['submitted_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['venue_id'], ['venues.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_id')
    )
    op.create_index('ix_events_status_end_date', 'events', ['status', 'end_date'], unique=False)
    op.create_index(op.f('ix_events_venue_id'), 'events', ['venue_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)
    op.create_table('attended',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'event_id')
    )


def downgrade():
    op.drop_table('attended')
    op.drop_index(op.f('ix_refresh_tokens_user_id'), table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index(op.f('ix_events_venue_id'), table_name='events')
    op.drop_index('ix_events_status_end_date', table_name='events')
    op.drop_table('events')
    op.drop_table('venues')
    op.drop_table('users')
    # Postgres keeps enum types after their tables are dropped; remove them so upgrade can run again.
    for enum_name in ('event_status', 'venue_area', 'user_role'):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
