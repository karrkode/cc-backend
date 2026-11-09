"""CC-245: the admins' Issues dashboard: open reports by status and age, new ones per week, the time to fix, and where
the open ones stand in Jira. Its counts must match the Issues list's."""
from datetime import UTC, datetime, timedelta

import pytest

from app import jira
from app.models import IssueReport
from app.routers import issues as issues_router
from app.stats import CHICAGO, today_in_chicago

from .conftest import auth

REPORT = {"what_happened": "The Tonight list was empty", "page": "Plan an outing"}


@pytest.fixture
def fake(monkeypatch):
    f = jira.FakeJira()
    monkeypatch.setattr(jira, "client", lambda: f)
    return f


def send(client, token, n=1, **extra):
    issues_router.report_limiter.reset()
    return [client.post("/issues", json={**REPORT, **extra}, headers=auth(token)).json() for _ in range(n)]


def stats(client, token, query=""):
    return client.get("/admin/issue-stats" + query, headers=auth(token))


def backdate(db, report_id, days, closed_days=None):
    r = db.get(IssueReport, report_id)
    r.created_at = datetime.now(UTC) - timedelta(days=days)
    if closed_days is not None:
        r.closed_at = datetime.now(UTC) - timedelta(days=closed_days)
    db.commit()


def test_admins_only(client, user_token):
    assert client.get("/admin/issue-stats").status_code == 401
    assert stats(client, user_token).status_code == 403


def test_nothing_reported_yet(client, admin_token, fake):
    s = stats(client, admin_token).json()
    assert s["counts"] == {} and s["open"] == 0 and s["open_by_kind"] == {}
    assert s["age"] == {"median_days": None, "oldest": None, "under_week": 0, "one_to_four_weeks": 0, "over_four_weeks": 0}
    assert len(s["weeks"]) == 8 and all(w["count"] == 0 for w in s["weeks"])
    assert s["fix_time"] == {"median_days": None, "fixed": 0, "last_30_days": 0}
    assert s["jira"]["available"] is True and s["jira"]["no_ticket"] == 0
    assert fake.calls == []  # nothing linked, so Jira isn't asked


def test_counts_match_the_issues_list(client, user_token, admin_token, fake):
    a, b, c = send(client, user_token, 3)
    (d,) = send(client, user_token, kind="feature")
    client.patch(f"/admin/issues/{b['id']}", json={"status": "looking"}, headers=auth(admin_token))
    client.patch(f"/admin/issues/{c['id']}", json={"status": "fixed"}, headers=auth(admin_token))
    for query in ("", "?kind=issue", "?kind=feature"):
        s = stats(client, admin_token, query).json()
        page = client.get("/admin/issues" + query, headers=auth(admin_token)).json()
        assert s["counts"] == page["counts"]
        assert s["open"] == sum(page["counts"].get(k, 0) for k in ("new", "looking"))
    s = stats(client, admin_token).json()
    assert s["counts"] == {"new": 2, "looking": 1, "fixed": 1} and s["open"] == 3
    assert s["open_by_kind"] == client.get("/admin/issues", headers=auth(admin_token)).json()["kinds"] == {"issue": 2, "feature": 1}
    assert stats(client, admin_token, "?kind=nope").status_code == 422


def test_age_of_open_reports(client, user_token, admin_token, db, fake):
    a, b, c, d = send(client, user_token, 4)
    backdate(db, a["id"], 2)
    backdate(db, b["id"], 10)
    backdate(db, c["id"], 40)
    backdate(db, d["id"], 90)
    client.patch(f"/admin/issues/{d['id']}", json={"status": "wont_fix"}, headers=auth(admin_token))  # closed: not counted
    age = stats(client, admin_token).json()["age"]
    assert (age["under_week"], age["one_to_four_weeks"], age["over_four_weeks"]) == (1, 1, 1)
    assert age["median_days"] == pytest.approx(10, abs=0.01)
    assert age["oldest"]["key"] == c["key"] and age["oldest"]["days"] == pytest.approx(40, abs=0.01)


def test_new_per_week_in_chicago(client, user_token, admin_token, db, fake):
    a, b, c = send(client, user_token, 3)
    backdate(db, b["id"], 7)
    backdate(db, c["id"], 100)  # older than the 8 weeks shown
    weeks = stats(client, admin_token).json()["weeks"]
    today = today_in_chicago()
    monday = today - timedelta(days=today.weekday())
    assert weeks[-1]["week"] == monday.isoformat() and weeks[0]["week"] == (monday - timedelta(weeks=7)).isoformat()
    assert weeks[-1]["count"] == 1 and weeks[-2]["count"] == 1 and sum(w["count"] for w in weeks) == 2
    created = db.get(IssueReport, a["id"]).created_at
    assert created.replace(tzinfo=created.tzinfo or UTC).astimezone(CHICAGO).date() >= monday


def test_time_to_fix(client, user_token, admin_token, db, fake):
    a, b, c, d = send(client, user_token, 4)
    for r in (a, b, c):
        client.patch(f"/admin/issues/{r['id']}", json={"status": "fixed"}, headers=auth(admin_token))
    client.patch(f"/admin/issues/{d['id']}", json={"status": "wont_fix"}, headers=auth(admin_token))  # not a fix
    backdate(db, a["id"], 10, closed_days=8)  # 2 days
    backdate(db, b["id"], 50, closed_days=40)  # 10 days, more than 30 days ago
    backdate(db, c["id"], 5, closed_days=1)  # 4 days
    fix = stats(client, admin_token).json()["fix_time"]
    assert fix["fixed"] == 3 and fix["last_30_days"] == 2
    assert fix["median_days"] == pytest.approx(4, abs=0.01)


def test_closed_at_follows_the_status(client, user_token, admin_token, db):
    (a,) = send(client, user_token)
    patch = lambda body: client.patch(f"/admin/issues/{a['id']}", json=body, headers=auth(admin_token))  # noqa: E731
    patch({"status": "fixed"})
    first = db.get(IssueReport, a["id"]).closed_at
    assert first is not None
    patch({"status": "wont_fix", "admin_note": "Changed my mind"})  # still closed: keeps when it was closed
    db.expire_all()
    assert db.get(IssueReport, a["id"]).closed_at == first
    patch({"status": "looking"})  # reopened
    db.expire_all()
    assert db.get(IssueReport, a["id"]).closed_at is None


def test_where_open_reports_stand_in_jira(client, user_token, admin_token, fake):
    a, b, c, d, e = send(client, user_token, 5)
    for r in (a, b, c, d, e):
        assert client.post(f"/admin/issues/{r['id']}/jira", json={"issue_type": "Bug", "summary": "Tonight list empty"},
                           headers=auth(admin_token)).status_code == 200
    keys = {r["id"]: client.get("/admin/issues?q=" + r["key"], headers=auth(admin_token)).json()["items"][0]["jira_key"] for r in (a, b, c, d, e)}
    fake.tickets[keys[b["id"]]].update(status="In Progress", category="indeterminate")
    fake.tickets[keys[c["id"]]].update(status="Done", category="done")
    del fake.tickets[keys[d["id"]]]
    client.patch(f"/admin/issues/{e['id']}", json={"status": "fixed"}, headers=auth(admin_token))  # closed: not counted
    send(client, user_token)  # open, no ticket
    j = stats(client, admin_token).json()["jira"]
    assert j == {"available": True, "message": "", "no_ticket": 1, "to_do": 1, "in_progress": 1, "done": 1, "not_found": 1}


def test_jira_down_leaves_the_rest(client, user_token, admin_token, fake):
    a, b = send(client, user_token, 2)
    client.post(f"/admin/issues/{a['id']}/jira", json={"issue_type": "Bug", "summary": "Tonight list empty"}, headers=auth(admin_token))
    fake.fail_next = jira.JiraError(504, "Jira didn't answer in time.")
    r = stats(client, admin_token)
    assert r.status_code == 200
    s = r.json()
    assert s["open"] == 2 and s["jira"] == {"available": False, "message": "Jira didn't answer in time.", "no_ticket": 1,
                                             "to_do": 0, "in_progress": 0, "done": 0, "not_found": 0}
