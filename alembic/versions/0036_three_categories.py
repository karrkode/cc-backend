"""three categories: Comedy and Educational go, with their events and those at The Second City and The Annoyance
(CC-133)

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-28 12:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None

REMOVED = "('Comedy', 'Educational')"
# The two comedy theatres, whatever the venue row is called ("The Second City", "Second City e.t.c.", "Annoyance")
AT_THEM = "(lower(name) LIKE '%second city%' OR lower(name) LIKE '%annoyance%')"


def upgrade() -> None:
    op.add_column('events', sa.Column('withdrawn_reason', sa.String(200), nullable=False, server_default=''))
    # Withdrawn, never deleted: the admin reviews them and deletes or brings back. Rejected and already-withdrawn
    # events are left alone. The reason is the category, or for any other category the venue's name.
    op.execute(f"""UPDATE events SET status = 'withdrawn', withdrawn_reason = CASE WHEN category IN {REMOVED} THEN category
        ELSE (SELECT name FROM venues v WHERE v.id = events.venue_id) END
        WHERE status IN ('approved', 'pending')
          AND (category IN {REMOVED} OR venue_id IN (SELECT id FROM venues WHERE {AT_THEM}))""")
    # The venues stay (members' journals point at them), without a default category
    op.execute(f"UPDATE venues SET category = NULL, opera = false WHERE category IN {REMOVED} OR {AT_THEM}")
    # Found events still waiting in a removed category are left out, as new ones will be
    op.execute(f"""UPDATE discovered_items SET status = 'skipped', skip_reason = category || ', which isn''t listed'
        WHERE status IN ('new', 'update') AND category IN {REMOVED}""")
    # Deleting a source (now possible on the Sources page) keeps what it found and its checks' cost
    for table in ('discovered_items', 'discovery_checks'):
        op.drop_constraint(f'{table}_source_id_fkey', table, type_='foreignkey')
        op.create_foreign_key(f'{table}_source_id_fkey', table, 'discovery_sources', ['source_id'], ['id'], ondelete='SET NULL')
    op.alter_column('discovered_items', 'source_id', existing_type=sa.Integer(), nullable=True)
    counts = op.get_bind().execute(sa.text("SELECT withdrawn_reason, count(*) FROM events WHERE withdrawn_reason != '' "
                                           "GROUP BY withdrawn_reason ORDER BY withdrawn_reason")).all()
    for reason, n in counts:
        print(f"CC-133: withdrew {n} event(s): {reason}")


def downgrade() -> None:
    # Sourceless rows have no place in the old tables
    op.execute("DELETE FROM discovered_items WHERE source_id IS NULL")
    op.execute("DELETE FROM discovery_checks WHERE source_id IS NULL AND what != 'link'")
    op.alter_column('discovered_items', 'source_id', existing_type=sa.Integer(), nullable=False)
    for table in ('discovered_items', 'discovery_checks'):
        op.drop_constraint(f'{table}_source_id_fkey', table, type_='foreignkey')
        op.create_foreign_key(f'{table}_source_id_fkey', table, 'discovery_sources', ['source_id'], ['id'], ondelete='CASCADE')
    # What was withdrawn here goes back on show (pending ones too: the old table can't tell them apart)
    op.execute("UPDATE events SET status = 'approved' WHERE withdrawn_reason != '' AND status = 'withdrawn'")
    op.drop_column('events', 'withdrawn_reason')
