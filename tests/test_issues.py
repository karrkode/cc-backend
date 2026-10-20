"""CC-84: Report an issue: signed-in members only; admins review, set a status and leave a note the member sees."""
from app.discovery.recap import build_recap
from app.routers import issues as issues_router

from .conftest import auth, register

REPORT = {"what_happened": "  The Tonight list was empty  ", "expected": "Some shows", "page": "Plan an outing",
          "browser": "Safari 19 on macOS", "screen": "1440x900", "site_version": "acae3a8c72"}


def test_members_report_and_see_their_own(client, user_token):
    issues_router.report_limiter.reset()
    r = client.post("/issues", json=REPORT, headers=auth(user_token))
    assert r.status_code == 201
    mine = client.get("/me/issues", headers=auth(user_token)).json()
    assert [(m["what_happened"], m["status"], m["admin_note"], m["page"]) for m in mine] == \
        [("The Tonight list was empty", "new", "", "Plan an outing")]
    assert "browser" not in mine[0]  # the member's own view is just what they wrote and our answer
    other = auth(register(client, email="bo@example.com", name="Bo"))
    assert client.get("/me/issues", headers=other).json() == []


def test_signed_out_visitors_cant_report(client):
    assert client.post("/issues", json=REPORT).status_code == 401
    assert client.get("/me/issues").status_code == 401


def test_admins_review_set_status_and_reply(client, user_token, admin_token):
    issues_router.report_limiter.reset()
    issue = client.post("/issues", json=REPORT, headers=auth(user_token)).json()
    page = client.get("/admin/issues", headers=auth(admin_token)).json()
    item = page["items"][0]
    assert page["counts"] == {"new": 1} and item["sent_by"]["email"] == "ana@example.com" and item["browser"] == "Safari 19 on macOS"
    assert client.get("/admin/issues", headers=auth(user_token)).status_code == 403
    r = client.patch(f"/admin/issues/{issue['id']}", json={"status": "fixed", "admin_note": " Fixed, thanks! "}, headers=auth(admin_token))
    assert (r.json()["status"], r.json()["admin_note"]) == ("fixed", "Fixed, thanks!")
    assert client.get("/me/issues", headers=auth(user_token)).json()[0]["admin_note"] == "Fixed, thanks!"
    assert client.get("/admin/issues?show=new", headers=auth(admin_token)).json()["items"] == []
    assert client.patch(f"/admin/issues/{issue['id']}", json={"status": "done"}, headers=auth(admin_token)).status_code == 422


def test_checks_and_rate_limit(client, user_token, monkeypatch):
    issues_router.report_limiter.reset()
    assert client.post("/issues", json={**REPORT, "what_happened": "x"}, headers=auth(user_token)).status_code == 422
    assert client.post("/issues", json={**REPORT, "what_happened": "y" * 2001}, headers=auth(user_token)).status_code == 422
    monkeypatch.setattr(issues_router, "REPORTS_PER_HOUR", 2)
    codes = [client.post("/issues", json=REPORT, headers=auth(user_token)).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
    issues_router.report_limiter.reset()


def test_new_reports_are_in_the_recap(client, user_token, db):
    issues_router.report_limiter.reset()
    client.post("/issues", json=REPORT, headers=auth(user_token))
    email = build_recap(db)
    assert "1 issue reported" in email.subject
    assert "ISSUES REPORTED THIS WEEK: 1 (1 open in all)" in email.text and "Ana on Plan an outing: The Tonight list was empty" in email.text
    assert "/#issues" in email.html and "The Tonight list was empty" in email.html
