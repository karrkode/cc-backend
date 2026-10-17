"""opt-in "suggested by" credit

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17 15:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    # Existing events default to no credit: nobody opted in before the choice existed.
    op.add_column('events', sa.Column('credit_me', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column('events', 'credit_me')
