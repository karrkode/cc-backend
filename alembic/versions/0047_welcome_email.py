"""when an account's welcome email went out (CC-243)

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-09 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('welcomed_at', sa.DateTime(timezone=True), nullable=True))
    # members confirmed before the welcome existed don't get it later (say, after changing their email)
    op.execute("UPDATE users SET welcomed_at = email_verified_at WHERE email_verified_at IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.drop_column('welcomed_at')
