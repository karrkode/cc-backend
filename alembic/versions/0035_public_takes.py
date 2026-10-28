"""a member can share a journal entry publicly on the event's page, and an admin can hide it (CC-112)

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-28 11:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('attended') as t:
        t.add_column(sa.Column('shared_at', sa.DateTime(timezone=True), nullable=True))
        t.add_column(sa.Column('anonymous', sa.Boolean(), server_default=sa.false(), nullable=False))
        t.add_column(sa.Column('hidden', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('attended') as t:
        t.drop_column('hidden')
        t.drop_column('anonymous')
        t.drop_column('shared_at')
