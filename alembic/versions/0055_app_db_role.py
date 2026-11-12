"""CC-200: a least-privilege database role for the running API.

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-11

The API connects as `culture_app`, which can read and write rows but can't change the schema. Migrations keep
running as the owner. The role is created here without a login or a password, so no secret is in the repo: the
deploy step (scripts/migrate.py) gives it its password from Secrets Manager. Roles are shared by every database on
a Postgres server (the local dev database and the test databases share one), so creation is idempotent and the
downgrade only revokes in this database.
"""
from alembic import op

revision = '0055'
down_revision = '0054'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'culture_app') "
               "THEN CREATE ROLE culture_app NOLOGIN; END IF; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("GRANT USAGE ON SCHEMA public TO culture_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO culture_app")
    # `alembic current` runs as the app role and only needs to read the revision.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM culture_app")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO culture_app")  # nextval for SERIAL ids
    # Tables and sequences that later migrations create (as the same owner) are covered too.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO culture_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO culture_app")


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    # Revoke here only. The role itself stays: other databases on the server may still grant to it.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE ON SEQUENCES FROM culture_app")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM culture_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM culture_app")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM culture_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM culture_app")
