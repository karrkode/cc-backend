"""sheet sync log and withdrawn status

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 22:12:39.484467
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    # Autogenerate doesn't see new enum values, so this one is written by hand.
    # ADD VALUE can't share a transaction with statements that use the new value, so it runs on its own.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_status ADD VALUE IF NOT EXISTS 'withdrawn'")

    op.create_table('sheet_syncs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', sa.String(length=20), nullable=False),
    sa.Column('rows_read', sa.Integer(), nullable=False),
    sa.Column('added', sa.Integer(), nullable=False),
    sa.Column('updated', sa.Integer(), nullable=False),
    sa.Column('unchanged', sa.Integer(), nullable=False),
    sa.Column('withdrawn', sa.Integer(), nullable=False),
    sa.Column('restored', sa.Integer(), nullable=False),
    sa.Column('skipped', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('sheet_syncs')
    # Postgres can't drop one value from an enum, so rebuild the type without it.
    # Withdrawn events become rejected: still hidden, which is the closest old status.
    op.execute("UPDATE events SET status = 'rejected' WHERE status = 'withdrawn'")
    op.execute("ALTER TYPE event_status RENAME TO event_status_old")
    op.execute("CREATE TYPE event_status AS ENUM ('pending', 'approved', 'rejected')")
    op.execute("ALTER TABLE events ALTER COLUMN status TYPE event_status USING status::text::event_status")
    op.execute("DROP TYPE event_status_old")
