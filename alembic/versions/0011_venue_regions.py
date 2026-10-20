"""venue regions: the side of town each venue is in

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

from app.areas import region_of
from app.models import Area


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

REGIONS = ('downtown', 'north', 'west', 'south', 'suburbs', 'out_of_town')


def upgrade():
    region = sa.Enum(*REGIONS, name='venue_region')
    region.create(op.get_bind(), checkfirst=True)
    op.add_column('venues', sa.Column('region', region, nullable=True))
    # Fill in every venue from its neighborhood; city neighborhoods not in the list stay empty for an admin.
    venues = sa.table('venues', sa.column('id', sa.Integer), sa.column('neighborhood', sa.String),
                      sa.column('area', sa.String), sa.column('region', region))
    conn = op.get_bind()
    for venue_id, hood, area in conn.execute(sa.select(venues.c.id, venues.c.neighborhood, venues.c.area)).all():
        r = region_of(hood, Area(area))
        if r is not None:
            conn.execute(venues.update().where(venues.c.id == venue_id).values(region=r.value))


def downgrade():
    op.drop_column('venues', 'region')
    sa.Enum(name='venue_region').drop(op.get_bind(), checkfirst=True)
