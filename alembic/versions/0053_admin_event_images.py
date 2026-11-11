"""CC-265: remember an admin's event-picture decision (including explicit removal).

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa

revision = '0053'
down_revision = '0052'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('events') as b:
        b.add_column(sa.Column('image_override', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('events') as b:
        b.drop_column('image_override')
