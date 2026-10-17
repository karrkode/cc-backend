import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Role, User
from app.security import hash_password, verify_password
from scripts import dev_admin

from .conftest import auth


def test_creates_an_admin_who_can_sign_in(client, db):
    assert dev_admin.ensure_dev_admin(db, "Admin@Example.com", "local-admin-only") == "created"
    r = client.post("/auth/login", json={"email": "admin@example.com", "password": "local-admin-only"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert client.get("/admin/events", headers=auth(r.json()["access_token"])).status_code == 200


def test_running_again_changes_nothing(db):
    dev_admin.ensure_dev_admin(db, "admin@example.com", "local-admin-only")
    before = db.scalar(select(User)).password_hash
    assert dev_admin.ensure_dev_admin(db, "admin@example.com", "local-admin-only") == "unchanged"
    assert db.scalar(select(User)).password_hash == before
    assert len(db.scalars(select(User)).all()) == 1


def test_puts_back_a_changed_password_and_role(db):
    db.add(User(email="admin@example.com", password_hash=hash_password("someone-changed-it"), role=Role.user))
    db.commit()
    assert dev_admin.ensure_dev_admin(db, "admin@example.com", "local-admin-only") == "updated"
    user = db.scalar(select(User))
    assert user.role == Role.admin and verify_password("local-admin-only", user.password_hash)


@pytest.mark.parametrize("env", ["prod", "test"])
def test_refuses_outside_local(monkeypatch, env):
    s = Settings(env=env, jwt_secret="x" * 40, dev_admin_email="admin@example.com", dev_admin_password="local-admin-only")
    monkeypatch.setattr(dev_admin, "get_settings", lambda: s)
    monkeypatch.setattr(dev_admin, "SessionLocal", lambda: pytest.fail("must not touch the database"))
    with pytest.raises(SystemExit, match="only runs with ENV=local"):
        dev_admin.main()


def test_needs_both_settings(monkeypatch):
    monkeypatch.setattr(dev_admin, "get_settings", lambda: Settings(env="local", dev_admin_email="admin@example.com"))
    with pytest.raises(SystemExit, match="DEV_ADMIN_EMAIL and DEV_ADMIN_PASSWORD"):
        dev_admin.main()
