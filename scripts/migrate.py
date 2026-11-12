"""CC-200: the deploy's migration step. Runs as the database owner, then hands the app role its password.

    OWNER_DATABASE_URL=...owner... DATABASE_URL=...app role... python -m scripts.migrate

1. `alembic upgrade head` as the owner.
2. ALTER ROLE <app user> WITH LOGIN PASSWORD <SCRAM verifier>. The user and password come from DATABASE_URL, which
   is what the API connects with, so the plain password never reaches the server or its logs.
3. Check the app role can do what the API needs (and no more) and exit non-zero if not.
"""
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

DATA_RIGHTS = ("SELECT", "INSERT", "UPDATE", "DELETE")


def migrate(owner_url: str) -> None:
    cfg = Config("alembic.ini")
    url = make_url(owner_url)
    if url.drivername.startswith("postgresql"):
        # The old API is still serving while this runs: give up after 5 seconds instead of queueing a schema
        # lock that would hold up every query on that table. The deploy fails and can simply be run again.
        url = url.update_query_dict({"options": "-c lock_timeout=5s"})
    cfg.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")


def set_app_password(owner_url: str, app_url: str) -> str:
    app = make_url(app_url)
    from psycopg import sql
    engine = create_engine(owner_url)
    try:
        with engine.connect() as conn:
            raw = conn.connection.dbapi_connection
            verifier = raw.pgconn.encrypt_password(app.password.encode(), app.username.encode(), b"scram-sha-256").decode()
            raw.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(app.username), sql.Literal(verifier)))
            raw.commit()
    finally:
        engine.dispose()
    return app.username


def check_rights(owner_url: str, role: str) -> list[str]:
    """Return what's wrong with the role's rights; empty means fine."""
    problems = []
    engine = create_engine(owner_url)
    try:
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))]
            for t in tables:
                # has_table_privilege with a list is true if ANY one is held, so ask one at a time.
                wanted = ("SELECT",) if t == "alembic_version" else DATA_RIGHTS
                for right in wanted:
                    if not conn.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                        {"r": role, "t": f"public.{t}", "p": right}).scalar():
                        problems.append(f"{role} lacks {right} on {t}")
                if t == "alembic_version":
                    for right in ("INSERT", "UPDATE", "DELETE"):
                        if conn.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                        {"r": role, "t": f"public.{t}", "p": right}).scalar():
                            problems.append(f"{role} may {right} alembic_version")
            for (seq,) in conn.execute(text("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")):
                if not conn.execute(text("SELECT has_sequence_privilege(:r, :s, 'USAGE')"),
                                    {"r": role, "s": f"public.{seq}"}).scalar():
                    problems.append(f"{role} lacks USAGE on sequence {seq}")
            if conn.execute(text("SELECT has_schema_privilege(:r, 'public', 'CREATE')"), {"r": role}).scalar():
                problems.append(f"{role} can create objects in schema public")
    finally:
        engine.dispose()
    return problems


def main() -> int:
    owner_url = os.environ.get("OWNER_DATABASE_URL")
    app_url = os.environ.get("DATABASE_URL")
    if not owner_url or not app_url:
        print("OWNER_DATABASE_URL and DATABASE_URL are both required", file=sys.stderr)
        return 2
    migrate(owner_url)
    role = set_app_password(owner_url, app_url)
    problems = check_rights(owner_url, role)
    if problems:
        print("The app role can't do its job:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    # Prove the app role can really sign in and read before the deploy stops the old API.
    app_engine = create_engine(app_url)
    try:
        with app_engine.connect() as conn:
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as e:  # noqa: BLE001
        # No role name here: it's parsed from the URL that holds the password, and scanners flag logging it.
        print(f"The app role can't sign in with DATABASE_URL: {type(e).__name__}", file=sys.stderr)
        return 1
    finally:
        app_engine.dispose()
    with create_engine(owner_url).connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        owner = conn.execute(text("SELECT current_user")).scalar()
    print(f"Schema at {rev} as {owner}; the app role can sign in")
    return 0


if __name__ == "__main__":
    sys.exit(main())
