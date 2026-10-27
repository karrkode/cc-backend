"""the five categories, opera, and a default category for each venue (CC-125)

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-27 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None

FIVE = "('Theater', 'Comedy', 'Music', 'Visual Art', 'Educational')"


def upgrade() -> None:
    for table in ('events', 'venues', 'discovered_items'):
        op.add_column(table, sa.Column('opera', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('venues', sa.Column('category', sa.String(40), nullable=True))
    # Concert is now Music; Theater, Comedy and Visual Art stay. Anything else is left for an admin to fix
    # (admin Events lists them), not guessed.
    op.execute("UPDATE events SET category = 'Music' WHERE category = 'Concert'")
    op.execute("UPDATE discovered_items SET category = 'Music' WHERE category = 'Concert'")
    # Found events still waiting from an opera source arrive as opera, as new ones will
    op.execute("UPDATE discovered_items SET category = 'Music', opera = true WHERE status IN ('new', 'skipped', 'rejected') "
               "AND source_id IN (SELECT id FROM discovery_sources WHERE kind = 'opera')")
    # Found events still waiting with no category take their source kind's (museum: Visual Art)
    op.execute("""UPDATE discovered_items SET category = (SELECT CASE s.kind WHEN 'theater' THEN 'Theater'
        WHEN 'music' THEN 'Music' WHEN 'comedy' THEN 'Comedy' WHEN 'museum' THEN 'Visual Art' END
        FROM discovery_sources s WHERE s.id = discovered_items.source_id)
        WHERE category IS NULL AND status IN ('new', 'skipped', 'rejected')""")
    # Each venue's default: its events' most common category (ties: alphabetical); none if it has no events
    op.execute(f"""UPDATE venues SET category = (
        SELECT e.category FROM events e WHERE e.venue_id = venues.id AND e.category IN {FIVE}
        GROUP BY e.category ORDER BY count(*) DESC, e.category LIMIT 1)""")
    left = op.get_bind().execute(sa.text(f"SELECT id, title, category FROM events WHERE category NOT IN {FIVE} ORDER BY id")).all()
    for id_, title, category in left:
        print(f"CC-125: event {id_} ({title!r}) has category {category!r}: fix it on admin Events")


def downgrade() -> None:
    op.execute("UPDATE events SET category = 'Concert' WHERE category = 'Music'")
    op.execute("UPDATE discovered_items SET category = 'Concert' WHERE category = 'Music'")
    op.drop_column('venues', 'category')
    for table in ('discovered_items', 'venues', 'events'):
        op.drop_column(table, 'opera')
