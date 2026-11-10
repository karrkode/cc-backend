"""an event's photo: its key in the event-images bucket and where it came from (CC-250)

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-10 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0051'
down_revision = '0050'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CC-250: empty for every event; scripts.event_images fills them from the reviewed manifest after the deploy.
    with op.batch_alter_table('events') as b:
        b.add_column(sa.Column('image_key', sa.String(length=200), nullable=True))
        b.add_column(sa.Column('image_source_url', sa.Text(), server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('events') as b:
        b.drop_column('image_source_url')
        b.drop_column('image_key')
