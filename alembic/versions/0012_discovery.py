"""event discovery: sources, found items, checks; the first 12 sources

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20 18:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

KINDS = ('theater', 'opera', 'museum', 'music', 'comedy')

# The product owner's first sources (CC-70). Monthly for theaters, opera and museums; every two
# weeks for music and comedy. Court and Lyric refuse automated visitors; they stay listed so each
# check logs that, for the product owner to look at by hand.
SOURCES = [
    # name, kind, reader, pages, every_days, venue name to link (if it exists) or add, neighborhood
    ("Art Institute of Chicago", "museum", "aic_api", ["https://api.artic.edu/api/v1"], 30, "Art Institute of Chicago", "Loop"),
    ("MCA Chicago", "museum", "claude", ["https://mcachicago.org/whats-on/exhibitions/", "https://mcachicago.org/whats-on/events/"], 30,
     "Museum of Contemporary Art Chicago (MCA)", "Streeterville"),
    ("Steppenwolf Theatre", "theater", "claude", ["https://www.steppenwolf.org/tickets--events/"], 30, "Steppenwolf Theatre", "Lincoln Park"),
    ("Court Theatre", "theater", "claude", ["https://www.courttheatre.org/season/"], 30, "Court Theatre", "Hyde Park"),
    ("Goodman Theatre", "theater", "claude", ["https://www.goodmantheatre.org/2026-2027-season/", "https://www.goodmantheatre.org/"], 30,
     "Goodman Theatre", "Loop"),  # the season page's address changes yearly; the home page links the next one
    ("Chicago Symphony Orchestra", "music", "claude", ["https://chicagosymphony.org/concerts-tickets/on-stage/"], 14, "Symphony Center", "Loop"),
    ("Lyric Opera of Chicago", "opera", "claude", ["https://www.lyricopera.org/shows/"], 30, "Lyric Opera House", "Loop"),
    ("First Floor Theater", "theater", "claude", ["https://www.firstfloortheater.com/"], 30, "First Floor Theater", ""),
    ("Field Museum", "museum", "claude", ["https://www.fieldmuseum.org/exhibitions", "https://www.fieldmuseum.org/events"], 30, "Field Museum", "South Loop"),
    ("Griffin Museum of Science and Industry", "museum", "claude", ["https://www.griffinmsi.org/exhibits", "https://www.griffinmsi.org/events"], 30,
     "Griffin Museum of Science and Industry", "Hyde Park"),
    ("The Second City", "comedy", "claude", ["https://www.secondcity.com/shows/chicago"], 14, "The Second City", "Old Town"),
    ("The Annoyance Theatre", "comedy", "claude", ["https://www.theannoyance.com/shows"], 14, "The Annoyance Theatre", "Uptown"),
]


def upgrade():
    kind = sa.Enum(*KINDS, name='source_kind')
    op.create_table(
        'discovery_sources',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False, unique=True),
        sa.Column('kind', kind, nullable=False),
        sa.Column('pages', sa.JSON(), nullable=False),
        sa.Column('reader', sa.String(length=20), nullable=False),
        sa.Column('venue_id', sa.Integer(), sa.ForeignKey('venues.id', ondelete='SET NULL'), nullable=True),
        sa.Column('venue_name', sa.String(length=200), nullable=False),
        sa.Column('neighborhood', sa.String(length=120), nullable=False),
        sa.Column('every_days', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('next_check_on', sa.Date(), nullable=True),
        sa.Column('unchanged_streak', sa.Integer(), nullable=False),
        sa.Column('page_state', sa.JSON(), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', sa.String(length=20), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=False),
        sa.Column('venue_hours', sa.JSON(), nullable=False),
    )
    op.create_table(
        'discovered_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('discovery_sources.id', ondelete='CASCADE'), nullable=False),
        sa.Column('key', sa.String(length=500), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('skip_reason', sa.String(length=200), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('url', sa.String(length=2000), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('category', sa.String(length=40), nullable=True),
        sa.Column('venue_name', sa.String(length=200), nullable=False),
        sa.Column('begin_date', sa.Date(), nullable=True),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('open_run', sa.Boolean(), nullable=False),
        sa.Column('cost_text', sa.String(length=120), nullable=False),
        sa.Column('min_price', sa.Numeric(8, 2), nullable=True),
        sa.Column('performances', sa.JSON(), nullable=False),
        sa.Column('changes', sa.JSON(), nullable=False),
        sa.Column('event_id', sa.Integer(), sa.ForeignKey('events.id', ondelete='SET NULL'), nullable=True),
        sa.Column('detail_checks', sa.JSON(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('source_id', 'key', name='uq_discovered_source_key'),
    )
    op.create_index('ix_discovered_items_source_id', 'discovered_items', ['source_id'])
    op.create_index('ix_discovered_items_status', 'discovered_items', ['status'])
    op.create_index('ix_discovered_items_event_id', 'discovered_items', ['event_id'])
    op.create_table(
        'discovery_checks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source_id', sa.Integer(), sa.ForeignKey('discovery_sources.id', ondelete='CASCADE'), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('what', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('new_items', sa.Integer(), nullable=False),
        sa.Column('updates', sa.Integer(), nullable=False),
        sa.Column('skipped', sa.Integer(), nullable=False),
        sa.Column('pages_read', sa.Integer(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
    )
    op.create_index('ix_discovery_checks_source_id', 'discovery_checks', ['source_id'])
    op.create_index('ix_discovery_checks_checked_at', 'discovery_checks', ['checked_at'])

    conn = op.get_bind()
    venues = sa.table('venues', sa.column('id', sa.Integer), sa.column('name', sa.String))
    sources = sa.table('discovery_sources', sa.column('name', sa.String), sa.column('kind', kind), sa.column('reader', sa.String),
                       sa.column('pages', sa.JSON), sa.column('venue_id', sa.Integer), sa.column('venue_name', sa.String),
                       sa.column('neighborhood', sa.String), sa.column('every_days', sa.Integer), sa.column('active', sa.Boolean),
                       sa.column('unchanged_streak', sa.Integer), sa.column('page_state', sa.JSON), sa.column('last_status', sa.String),
                       sa.column('last_error', sa.Text), sa.column('venue_hours', sa.JSON))
    for name, k, reader, pages, every, venue_name, hood in SOURCES:
        venue_id = conn.scalar(sa.select(venues.c.id).where(sa.func.lower(venues.c.name) == venue_name.lower()).order_by(venues.c.id))
        conn.execute(sources.insert().values(name=name, kind=k, reader=reader, pages=pages, venue_id=venue_id, venue_name=venue_name,
                                             neighborhood=hood, every_days=every, active=True, unchanged_streak=0, page_state={},
                                             last_status="", last_error="", venue_hours={}))


def downgrade():
    op.drop_table('discovery_checks')
    op.drop_table('discovered_items')
    op.drop_table('discovery_sources')
    sa.Enum(name='source_kind').drop(op.get_bind(), checkfirst=True)
