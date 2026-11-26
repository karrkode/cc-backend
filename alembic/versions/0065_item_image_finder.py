"""CC-320: which version of the picture finder last looked at a found item, so one an older finder missed gets another look.

Revision ID: 0065
Revises: 0064
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = '0065'
down_revision = '0064'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One new defaulted column: no existing row is rewritten (they read 0, "an older finder"). The API (culture_app)
    # can't change the schema, so it is a migration. Reversible: the downgrade drops the column. The site doesn't read it.
    op.add_column('discovered_items', sa.Column('image_finder', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('discovered_items', 'image_finder')
