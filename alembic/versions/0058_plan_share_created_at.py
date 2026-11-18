"""CC-240: when a plan was shared with a crew, for the growth console's weekly numbers.

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-18
"""
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa

revision = '0058'
down_revision = '0057'
branch_labels = None
depends_on = None

MARKER = 'plan_shares_timed_since'


def upgrade() -> None:
    # Added with no default, so shares made before this stay null (a default would stamp them all "now" and put
    # them in this week's count); the default is set afterwards, for new rows only.
    op.add_column('plan_shares', sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))
    op.alter_column('plan_shares', 'created_at', server_default=sa.func.now())
    # The marker tells the console endpoint which weeks it can count: only those that start after this moment.
    settings = sa.table('app_settings', sa.column('key', sa.String), sa.column('value', sa.JSON))
    op.bulk_insert(settings, [{'key': MARKER, 'value': {'at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}}])


def downgrade() -> None:
    op.execute(f"DELETE FROM app_settings WHERE key = '{MARKER}'")
    op.drop_column('plan_shares', 'created_at')
