"""scheduled publishing

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 00:08:16.931787
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('events', sa.Column('publish_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('events', 'publish_at')
