"""the API's own request and database-check records for the Site health page (CC-244)

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-09 16:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0048'
down_revision = '0047'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'request_metrics',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('method', sa.String(length=8), nullable=False),
        sa.Column('route', sa.String(length=200), nullable=False),
        sa.Column('status', sa.Integer(), nullable=False),
        sa.Column('ms', sa.Float(), nullable=False),
    )
    op.create_index('ix_request_metrics_at', 'request_metrics', ['at'])
    op.create_index('ix_request_metrics_status', 'request_metrics', ['status'])
    op.create_table(
        'db_checks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('ms', sa.Float(), nullable=True),
    )
    op.create_index('ix_db_checks_at', 'db_checks', ['at'])


def downgrade() -> None:
    op.drop_index('ix_db_checks_at', table_name='db_checks')
    op.drop_table('db_checks')
    op.drop_index('ix_request_metrics_status', table_name='request_metrics')
    op.drop_index('ix_request_metrics_at', table_name='request_metrics')
    op.drop_table('request_metrics')
