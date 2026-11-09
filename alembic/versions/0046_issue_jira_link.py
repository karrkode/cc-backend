"""the Jira ticket made from a member's report (CC-241)

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-09 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0046'
down_revision = '0045'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('issue_reports') as b:
        b.add_column(sa.Column('jira_key', sa.String(length=20), nullable=True))
        b.add_column(sa.Column('jira_created_at', sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column('jira_pending_at', sa.DateTime(timezone=True), nullable=True))
        b.create_unique_constraint('uq_issue_reports_jira_key', ['jira_key'])


def downgrade() -> None:
    with op.batch_alter_table('issue_reports') as b:
        b.drop_constraint('uq_issue_reports_jira_key', type_='unique')
        b.drop_column('jira_pending_at')
        b.drop_column('jira_created_at')
        b.drop_column('jira_key')
