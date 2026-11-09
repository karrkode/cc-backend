"""when a member's report was closed (fixed or won't fix), for the Issues dashboard's time to fix (CC-245)

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-09 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0049'
down_revision = '0048'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('issue_reports') as b:
        b.add_column(sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True))
    # CC-245: reports closed before this have no closing time; their last change is the best guess there is.
    op.execute("UPDATE issue_reports SET closed_at = updated_at WHERE status IN ('fixed', 'wont_fix')")


def downgrade() -> None:
    with op.batch_alter_table('issue_reports') as b:
        b.drop_column('closed_at')
