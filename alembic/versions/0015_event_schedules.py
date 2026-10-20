"""event schedules for the planner: performance dates and a weekly pattern (CC-79)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-20 21:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('events', sa.Column('performances', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('events', sa.Column('weekly', sa.JSON(), nullable=False, server_default='{}'))
    # Events already matched or approved from discovery get the dates and times it found.
    op.execute("""
        UPDATE events SET performances = d.performances
        FROM (SELECT DISTINCT ON (event_id) event_id, performances FROM discovered_items
              WHERE event_id IS NOT NULL AND performances::text NOT IN ('[]', 'null')
              ORDER BY event_id, last_seen_at DESC) AS d
        WHERE events.id = d.event_id
    """)


def downgrade() -> None:
    op.drop_column('events', 'weekly')
    op.drop_column('events', 'performances')
