"""Cultural Journal entries on "I've been" marks (CC-98)

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-21 21:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('attended', sa.Column('words', sa.String(62), server_default='', nullable=False))
    op.add_column('attended', sa.Column('review', sa.String(1000), server_default='', nullable=False))
    op.add_column('attended', sa.Column('recommend', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('attended', 'recommend')
    op.drop_column('attended', 'review')
    op.drop_column('attended', 'words')
