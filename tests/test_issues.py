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
    assert "ISSUES AND FEATURE REQUESTS THIS WEEK: 1 (1 open in all)" in email.text and "ISS-0001 Ana on Plan an outing: The Tonight list was empty" in email.text
    assert "/#issues/ISS-0001" in email.html
    assert "/#issues" in email.html and "The Tonight list was empty" in email.html


def test_every_report_has_a_ticket_number_admins_can_search(client, user_token, admin_token):
    issues_router.report_limiter.reset()
    first = client.post("/issues", json=REPORT, headers=auth(user_token)).json()
    second = client.post("/issues", json={**REPORT, "what_happened": "The dolphin overlaps the date"}, headers=auth(user_token)).json()
    assert (first["key"], second["key"]) == (f"ISS-{first['id']:04d}", f"ISS-{second['id']:04d}")
    assert client.get("/me/issues", headers=auth(user_token)).json()[0]["key"] == second["key"]
    find = lambda q: [i["key"] for i in client.get("/admin/issues", params={"q": q}, headers=auth(admin_token)).json()["items"]]
    for q in (second["key"], second["key"].lower(), str(second["id"]), f"#{second['id']}", f"iss {second['id']}"):
        assert find(q) == [second["key"]], q
    assert find("dolphin") == [second["key"]]  # words in the report
    assert find("ana@example") == [second["key"], first["key"]]  # the sender's email, newest first
    assert find("ISS-9999") == []


def test_feature_requests_are_numbered_apart_and_filtered(client, user_token, admin_token, db):
    """CC-95: a report can be a feature request: FR- numbers of its own, a kind filter, and in the recap."""
    issues_router.report_limiter.reset()
    h, admin = auth(user_token), auth(admin_token)
    bug = client.post("/issues", json=REPORT, headers=h).json()
    idea = client.post("/issues", json={"kind": "feature", "what_happened": "A map of the venues", "expected": "To plan by area"}, headers=h).json()
    bug2 = client.post("/issues", json={**REPORT, "what_happened": "The dolphin overlaps"}, headers=h).json()
    assert [(r["key"], r["kind"]) for r in (bug, idea, bug2)] == [("ISS-0001", "issue"), ("FR-0001", "feature"), ("ISS-0002", "issue")]
    assert client.post("/issues", json={**REPORT, "kind": "praise"}, headers=h).status_code == 422
    assert [m["key"] for m in client.get("/me/issues", headers=h).json()] == ["ISS-0002", "FR-0001", "ISS-0001"]
    keys = lambda **p: [i["key"] for i in client.get("/admin/issues", params=p, headers=admin).json()["items"]]
    assert keys(kind="feature") == ["FR-0001"] and keys(kind="issue") == ["ISS-0002", "ISS-0001"]
    assert keys(q="FR-1") == ["FR-0001"] and keys(q="fr 0001") == ["FR-0001"] and keys(q="ISS-1") == ["ISS-0001"]
    assert keys(q="1") == ["FR-0001", "ISS-0001"]  # a bare number finds it in either kind
    client.patch(f"/admin/issues/{bug['id']}", json={"status": "fixed"}, headers=admin)
    page = client.get("/admin/issues", headers=admin).json()
    assert page["counts"] == {"new": 2, "fixed": 1} and page["kinds"] == {"issue": 1, "feature": 1}  # kinds: open ones
    assert client.get("/admin/issues", params={"kind": "feature"}, headers=admin).json()["counts"] == {"new": 1}
    email = build_recap(db)
    assert "2 issues reported, 1 feature requested" in email.subject
    assert "FR-0001 Ana on -: A map of the venues" in email.text and "/#issues/FR-0001" in email.html


def test_old_reports_keep_their_numbers(client, user_token, db):
    """Reports from before CC-95 are issues numbered by their id (the migration sets number = id); new ones follow."""
    from app.models import IssueReport
    db.add(IssueReport(kind="issue", number=7, what_happened="From before", user_id=None))
    db.commit()
    issues_router.report_limiter.reset()
    assert client.post("/issues", json=REPORT, headers=auth(user_token)).json()["key"] == "ISS-0008"
