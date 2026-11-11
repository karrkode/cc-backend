"""sourcing's looks for an event's picture (CC-246)

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-10 17:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0052'
down_revision = '0051'
branch_labels = None
depends_on = None

MAX_TRIES = 3  # app.discovery.pictures.MAX_TRIES, copied so this file never changes with it


def upgrade() -> None:
    with op.batch_alter_table('events') as b:
        b.add_column(sa.Column('picture_tries', sa.Integer(), server_default='0', nullable=False))
        b.add_column(sa.Column('picture_checked_at', sa.DateTime(timezone=True), nullable=True))
    # CC-246: CC-250's search already looked for every existing event's picture (CC-247), so only new events are looked for
    op.execute(f"UPDATE events SET picture_tries = {MAX_TRIES}")


def downgrade() -> None:
    with op.batch_alter_table('events') as b:
        b.drop_column('picture_checked_at')
        b.drop_column('picture_tries')
