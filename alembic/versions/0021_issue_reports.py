"""members' issue reports (CC-84)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-22 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'issue_reports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('what_happened', sa.Text(), nullable=False),
        sa.Column('expected', sa.Text(), nullable=False, server_default=''),
        sa.Column('page', sa.String(200), nullable=False, server_default=''),
        sa.Column('browser', sa.String(300), nullable=False, server_default=''),
        sa.Column('screen', sa.String(30), nullable=False, server_default=''),
        sa.Column('site_version', sa.String(40), nullable=False, server_default=''),
        sa.Column('status', sa.String(20), nullable=False, server_default='new'),
        sa.Column('admin_note', sa.String(500), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_issue_reports_user_id', 'issue_reports', ['user_id'])
    op.create_index('ix_issue_reports_status', 'issue_reports', ['status'])


def downgrade() -> None:
    op.drop_table('issue_reports')
