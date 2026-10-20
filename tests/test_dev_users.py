"""CC-41 / CC-45: the local test accounts (scripts/dev_users.py)."""
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.models import Role, User
from app.security import hash_password, verify_password
from scripts import dev_users

from .conftest import auth

ADMIN = ("admin@example.com", "local-admin-only", Role.admin, "Local Admin")
USER = ("user@example.com", "local-user-only", Role.user, "Local User")


def local(**kw):
    base = dict(env="local", dev_admin_email=ADMIN[0], dev_admin_password=ADMIN[1],
                dev_user_email=USER[0], dev_user_password=USER[1])
    return Settings(**{**base, **kw})


def run(monkeypatch, Session, settings, capsys=None):
    monkeypatch.setattr(dev_users, "get_settings", lambda: settings)
    monkeypatch.setattr(dev_users, "SessionLocal", Session)
    dev_users.main()
    return capsys.readouterr().out if capsys else None


def test_creates_both_accounts_ready_to_use(client, monkeypatch, Session, capsys):
    out = run(monkeypatch, Session, local(), capsys)
    assert out.startswith("Local admin admin@example.com: created.\nLocal user user@example.com: created.\n")
    for email, password, role, name in (ADMIN, USER):
        r = client.post("/auth/login", json={"email": email, "password": password})
        me = r.json()["user"]
        assert r.status_code == 200 and (me["role"], me["display_name"], me["email_verified"]) == (role.value, name, True)
        assert me["dolphins"] is (role == Role.admin)  # CC-61: the admin can try the dolphins, the user the plain look
    user_token = client.post("/auth/login", json={"email": USER[0], "password": USER[1]}).json()["access_token"]
    assert client.get("/admin/events", headers=auth(user_token)).status_code == 403  # really a regular user


def test_running_again_changes_nothing(monkeypatch, Session, db, capsys):
    run(monkeypatch, Session, local(), capsys)
    before = {u.email: u.password_hash for u in db.scalars(select(User))}
    out = run(monkeypatch, Session, local(), capsys)
    assert out.count(": unchanged.") == 5  # the two accounts and the three crews
    db.expire_all()
    assert {u.email: u.password_hash for u in db.scalars(select(User))} == before


def test_puts_back_a_changed_password_role_or_unconfirmed_email(db):
    db.add(User(email=USER[0], password_hash=hash_password("someone-changed-it"), role=Role.admin, dolphins=True))
    db.commit()
    assert dev_users.ensure_dev_user(db, *USER) == "updated"
    user = db.scalar(select(User).where(User.email == USER[0]))
    assert user.role == Role.user and verify_password(USER[1], user.password_hash) and user.email_verified
    assert not user.dolphins


def test_either_account_can_be_left_out(monkeypatch, Session, db, capsys):
    out = run(monkeypatch, Session, local(dev_admin_email="", dev_admin_password=""), capsys)
    assert out == "Local user user@example.com: created.\n"
    assert db.scalar(select(func.count(User.id))) == 1


@pytest.mark.parametrize("env", ["prod", "test"])
def test_refuses_outside_local(monkeypatch, env):
    s = local(env=env, jwt_secret="x" * 40)
    monkeypatch.setattr(dev_users, "get_settings", lambda: s)
    monkeypatch.setattr(dev_users, "SessionLocal", lambda: pytest.fail("must not touch the database"))
    with pytest.raises(SystemExit, match="only runs with ENV=local"):
        dev_users.main()


@pytest.mark.parametrize("change, message", [
    (dict(dev_user_password=""), "Set both DEV_USER_EMAIL and DEV_USER_PASSWORD"),
    (dict(dev_admin_email=""), "Set both DEV_ADMIN_EMAIL and DEV_ADMIN_PASSWORD"),
    (dict(dev_user_email="Admin@Example.com"), "must be different addresses"),
    (dict(dev_admin_email="", dev_admin_password="", dev_user_email="", dev_user_password=""), "first"),
])
def test_settings_must_make_sense(monkeypatch, change, message):
    monkeypatch.setattr(dev_users, "get_settings", lambda: local(**change))
    monkeypatch.setattr(dev_users, "SessionLocal", lambda: pytest.fail("must not touch the database"))
    with pytest.raises(SystemExit, match=message):
        dev_users.main()


def test_ready_made_crews(client, monkeypatch, Session, db, capsys):
    """CC-74: three crews; the admin in all of them; seven members, each in at least one; three or more in each."""
    from app.models import Crew
    out = run(monkeypatch, Session, local(), capsys)
    assert "Crew Thursday Theater: created." in out and "Crew Laughin Times: created." in out
    admin = auth(client.post("/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).json()["access_token"])
    crews = {c["name"]: c for c in client.get("/crews", headers=admin).json()}
    assert list(crews) == ["Thursday Theater", "The Visual Gaze", "Laughin Times"]
    assert all(len(c["members"]) >= 3 for c in crews.values())
    assert [c["owner"] for c in crews.values()] == [True, False, False]  # the admin owns one, so both views can be tried
    named = {m["name"] for c in crews.values() for m in c["members"]} - {"Local Admin"}
    assert named == set(dev_users.CREW_MEMBERS.values())
    # members sign in with the test user's password; the test user is in no crew
    r = client.post("/auth/login", json={"email": "rosa@example.com", "password": USER[1]})
    assert r.status_code == 200 and [c["name"] for c in client.get("/crews", headers=auth(r.json()["access_token"])).json()] == \
        ["The Visual Gaze", "Laughin Times"]
    user = auth(client.post("/auth/login", json={"email": USER[0], "password": USER[1]}).json()["access_token"])
    assert client.get("/crews", headers=user).json() == []
    # someone who left is put back on the next start; nothing is duplicated
    client.post(f"/crews/{crews['Laughin Times']['id']}/leave", headers=admin)
    out = run(monkeypatch, Session, local(), capsys)
    assert "Crew Laughin Times: 1 put back." in out
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Crew)) == 3
