"""feature requests beside issue reports, numbered apart (CC-95)

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-21 22:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('issue_reports', sa.Column('kind', sa.String(10), server_default='issue', nullable=False))
    op.add_column('issue_reports', sa.Column('number', sa.Integer(), nullable=True))
    op.execute("UPDATE issue_reports SET number = id")  # every report so far is an issue, and keeps the ISS- number it was given
    op.alter_column('issue_reports', 'number', nullable=False)
    op.create_unique_constraint('uq_issue_reports_kind_number', 'issue_reports', ['kind', 'number'])
    op.create_index('ix_issue_reports_kind', 'issue_reports', ['kind'])


def downgrade() -> None:
    op.drop_index('ix_issue_reports_kind', 'issue_reports')
    op.drop_constraint('uq_issue_reports_kind_number', 'issue_reports', type_='unique')
    op.drop_column('issue_reports', 'number')
    op.drop_column('issue_reports', 'kind')
