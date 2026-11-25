"""CC-310: a found item carries a candidate picture, so the review screen shows it before approval.

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-24
"""
from alembic import op
import sqlalchemy as sa

revision = '0064'
down_revision = '0063'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only new columns, all nullable or defaulted, so no existing row changes. The API (culture_app) can't change the
    # schema, so it has to be a migration. Reversible: the downgrade drops the columns (the stored files stay in S3).
    op.add_column('discovered_items', sa.Column('image_key', sa.String(200), nullable=True))
    op.add_column('discovered_items', sa.Column('image_source_url', sa.Text(), server_default='', nullable=False))
    op.add_column('discovered_items', sa.Column('image_note', sa.String(300), server_default='', nullable=False))
    op.add_column('discovered_items', sa.Column('image_override', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('discovered_items', sa.Column('image_tries', sa.Integer(), server_default='0', nullable=False))
    op.add_column('discovered_items', sa.Column('image_checked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ('image_checked_at', 'image_tries', 'image_override', 'image_note', 'image_source_url', 'image_key'):
        op.drop_column('discovered_items', col)
