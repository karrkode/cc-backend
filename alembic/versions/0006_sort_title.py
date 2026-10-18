"""sort_title for alphabetical ordering on the server

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18 06:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

from app.models import sort_title


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('events', sa.Column('sort_title', sa.String(length=300), nullable=True))
    events = sa.table('events', sa.column('id', sa.Integer), sa.column('title', sa.String),
                      sa.column('sort_title', sa.String))
    conn = op.get_bind()
    for event_id, title in conn.execute(sa.select(events.c.id, events.c.title)).all():
        conn.execute(events.update().where(events.c.id == event_id).values(sort_title=sort_title(title)))
    op.alter_column('events', 'sort_title', nullable=False)
    op.create_index(op.f('ix_events_sort_title'), 'events', ['sort_title'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_events_sort_title'), table_name='events')
    op.drop_column('events', 'sort_title')
