"""a member's link read for the Suggest form is logged as a discovery check with no source (CC-124)

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-27 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('discovery_checks') as t:
        t.alter_column('source_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM discovery_checks WHERE source_id IS NULL")  # link reads have no place in the old table
    with op.batch_alter_table('discovery_checks') as t:
        t.alter_column('source_id', existing_type=sa.Integer(), nullable=False)
