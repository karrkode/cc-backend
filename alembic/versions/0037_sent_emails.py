"""a record of the emails members cause, for the per-address and per-account caps (CC-184)

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-02 19:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sent_emails',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('account_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_sent_emails_email_sent_at', 'sent_emails', ['email', 'sent_at'])
    op.create_index('ix_sent_emails_account_kind_sent_at', 'sent_emails', ['account_id', 'kind', 'sent_at'])


def downgrade() -> None:
    op.drop_table('sent_emails')
