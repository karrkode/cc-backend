"""discovery budget: site settings, and Check now requests that run past the budget (CC-80)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-21 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(60), primary_key=True),
        sa.Column('value', sa.JSON(), nullable=False),
    )
    op.add_column('discovery_sources', sa.Column('check_requested', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('discovery_sources', 'check_requested')
    op.drop_table('app_settings')
