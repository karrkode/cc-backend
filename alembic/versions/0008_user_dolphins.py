"""per-account plan dolphins

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    # Off for everyone; the owner turns it on for their accounts with scripts.dolphins.
    op.add_column('users', sa.Column('dolphins', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column('users', 'dolphins')
