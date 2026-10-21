"""photos with "I've been", and their Rekognition checks (CC-76)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-21 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'went_photos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(200), nullable=False, unique=True),
        sa.Column('content_type', sa.String(40), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='waiting'),
        sa.Column('blurred', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('labels', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('reason', sa.String(300), nullable=False, server_default=''),
        sa.Column('reviewed_by', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id', 'event_id'], ['attended.user_id', 'attended.event_id'], ondelete='CASCADE'),
        sa.UniqueConstraint('user_id', 'event_id', name='uq_went_photos_mark'),
    )
    op.create_index('ix_went_photos_event_id', 'went_photos', ['event_id'])
    op.create_index('ix_went_photos_status', 'went_photos', ['status'])
    op.create_table(
        'photo_checks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('outcome', sa.String(20), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_photo_checks_user_id', 'photo_checks', ['user_id'])
    op.create_index('ix_photo_checks_checked_at', 'photo_checks', ['checked_at'])


def downgrade() -> None:
    op.drop_table('photo_checks')
    op.drop_table('went_photos')
