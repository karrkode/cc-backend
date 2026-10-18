"""email verification and password-reset tokens

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18 03:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

purpose = sa.Enum('verify_email', 'reset_password', name='email_token_purpose')


def upgrade():
    # Existing accounts start unverified; they're asked to confirm their address, not locked out.
    op.add_column('users', sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'email_tokens',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('purpose', purpose, nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index(op.f('ix_email_tokens_user_id'), 'email_tokens', ['user_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_email_tokens_user_id'), table_name='email_tokens')
    op.drop_table('email_tokens')
    purpose.drop(op.get_bind(), checkfirst=True)  # create_table made the type; drop_table leaves it
    op.drop_column('users', 'email_verified_at')
