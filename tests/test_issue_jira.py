"""CC-241: an admin turns a member's report into exactly one Jira ticket, with personal details taken out, and sees
its status in Jira. Jira is the in-memory FakeJira; the real adapter is checked against a stubbed urlopen."""
import io
import json
import logging
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app import jira
from app.config import get_settings
from app.models import IssueReport
from app.routers import issues as issues_router

from .conftest import IS_POSTGRES, auth

REPORT = {"what_happened": "The Tonight list was empty. Mail me at ana.b@example.com or 312-555-0199",
          "expected": "Shows, like on https://chiculturecrew.com/#invite/abcDEF123?x=1",
          "page": "Plan an outing", "browser": "Safari 19 on macOS", "screen": "1440x900", "site_version": "acae3a8c72"}


@pytest.fixture
def fake(monkeypatch):
    f = jira.FakeJira()
    monkeypatch.setattr(jira, "client", lambda: f)
    return f


@pytest.fixture
def report(client, user_token):
    issues_router.report_limiter.reset()
    return client.post("/issues", json=REPORT, headers=auth(user_token)).json()


def draft(client, admin_token, report_id):
    return client.get(f"/admin/issues/{report_id}/jira", headers=auth(admin_token)).json()


def test_redact_takes_out_personal_details():
    text = ("Ana Lopez here, ana@example.com, (312) 555-0199, +1 312.555.0199, token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl "
            "and reset_q7Wm2Xp9Lk4Rt8Yv3Zn6Bc1Hd5Jf0Gs, see https://x.com/a/b?session=1#frag; version acae3a8c72, 1440x900")
    out = jira.redact(text, "Ana Lopez")
    for gone in ("ana@example.com", "555-0199", "555.0199", "eyJ", "q7Wm2Xp9", "session=1", "#frag", "Ana Lopez"):
        assert gone not in out
    assert "https://x.com/a/b" in out and "acae3a8c72" in out and "1440x900" in out and "[name removed]" in out


def test_draft_suggests_type_and_leaves_out_who_sent_it(client, admin_token, user_token, report):
    d = draft(client, admin_token, report["id"])
    assert (d["issue_type"], d["priority"]) == ("Bug", "Medium")
    assert d["summary"].startswith("ISS-0001: The Tonight list was empty.")
    assert "What happened\n" in d["description"] and "What they expected\n" in d["description"]
    assert "Page: Plan an outing" in d["description"] and "Site version: acae3a8c72" in d["description"]
    whole = json.dumps(d)
    for private in ("ana.b@example.com", "ana@example.com", "312-555-0199", "#invite", "abcDEF123?x=1"):
        assert private not in whole
    assert "/#issues/ISS-0001" in d["reference"] and "From Culture Crew report ISS-0001" in d["reference"]
    issues_router.report_limiter.reset()
    fr = client.post("/issues", json={"kind": "feature", "what_happened": "A map view", "expected": "To find shows nearby"},
                     headers=auth(user_token)).json()
    d = draft(client, admin_token, fr["id"])
    assert d["issue_type"] == "Story" and d["summary"] == "FR-0001: A map view" and d["description"].startswith("The idea\nA map view")


def test_only_admins(client, user_token, report, fake):
    assert client.get(f"/admin/issues/{report['id']}/jira", headers=auth(user_token)).status_code == 403
    assert client.post(f"/admin/issues/{report['id']}/jira", json={"issue_type": "Bug", "summary": "Hi there"},
                       headers=auth(user_token)).status_code == 403
    assert client.get(f"/admin/jira-status?ids={report['id']}", headers=auth(user_token)).status_code == 403
    assert client.post(f"/admin/issues/{report['id']}/jira", json={"issue_type": "Bug", "summary": "Hi there"}).status_code == 401
    assert fake.calls == []
    mine = client.get("/me/issues", headers=auth(user_token)).json()[0]
    assert "jira_key" not in mine and "jira_url" not in mine


def test_happy_path_makes_one_ticket_and_links_it(client, admin_token, user_token, report, fake):
    d = draft(client, admin_token, report["id"])
    before = client.get("/me/issues", headers=auth(user_token)).json()[0]["updated_at"]
    body = {"issue_type": "Task", "priority": "High", "summary": d["summary"],
            "description": d["description"] + "\nAlso call me at 773-555-0100 - Ana"}
    r = client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["jira_key"] == "CC-9001" and r.json()["jira_url"] == "https://chiculturecrew.atlassian.net/browse/CC-9001"
    t = fake.tickets["CC-9001"]
    assert (t["issue_type"], t["priority"], t["labels"]) == ("Task", "High", ["member-feedback", "report-ISS-0001"])
    assert "773-555-0100" not in t["description"] and "[phone removed]" in t["description"]  # the admin's text is checked too
    assert "Ana" not in t["description"].replace("[name removed]", "")
    assert t["description"].endswith("/#issues/ISS-0001")  # the reference back can't be edited out
    # A repeat click shows the same ticket and calls Jira no more
    calls = list(fake.calls)
    again = client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token))
    assert again.json()["jira_key"] == "CC-9001" and fake.calls == calls and len(fake.tickets) == 1
    item = client.get("/admin/issues", headers=auth(admin_token)).json()["items"][0]
    assert item["jira_key"] == "CC-9001" and item["status"] == "new"
    assert client.get("/me/issues", headers=auth(user_token)).json()[0]["updated_at"] == before  # nothing changed for the member


def test_status_comes_from_jira_and_done_doesnt_mark_fixed(client, admin_token, report, fake):
    client.post(f"/admin/issues/{report['id']}/jira", json={"issue_type": "Bug", "summary": "The Tonight list"}, headers=auth(admin_token))
    assert client.get(f"/admin/jira-status?ids={report['id']}&ids=999", headers=auth(admin_token)).json() == {
        str(report["id"]): {"key": "CC-9001", "url": "https://chiculturecrew.atlassian.net/browse/CC-9001", "status": "Idea", "category": "new"}}
    fake.tickets["CC-9001"].update(status="Done", category="done")  # merged and deployed
    got = client.get(f"/admin/jira-status?ids={report['id']}", headers=auth(admin_token)).json()
    assert got[str(report["id"])]["category"] == "done"
    assert client.get("/admin/issues", headers=auth(admin_token)).json()["items"][0]["status"] == "new"  # still the admin's call
    del fake.tickets["CC-9001"]  # deleted in Jira
    assert client.get(f"/admin/jira-status?ids={report['id']}", headers=auth(admin_token)).json() == {}
    fake.fail_next = jira.JiraError(504, "Jira didn't answer in time.")
    r = client.get(f"/admin/jira-status?ids={report['id']}", headers=auth(admin_token))
    assert r.status_code == 504 and r.json()["detail"] == "Jira didn't answer in time."


@pytest.mark.parametrize("err,code,words", [
    (jira.JiraError(502, "Jira refused the server's credentials (401)."), 502, "credentials"),
    (jira.JiraError(503, "Jira is limiting requests. Try again in 30 seconds.", retry_after=30), 503, "30 seconds"),
    (jira.JiraError(422, "Jira didn't accept the ticket: priority: invalid"), 422, "priority"),
])
def test_clear_failures_can_be_tried_again_at_once(client, admin_token, report, fake, err, code, words):
    fake.fail_next = err
    body = {"issue_type": "Bug", "summary": "The Tonight list"}
    r = client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token))
    assert r.status_code == code and words in r.json()["detail"] and "still here" in r.json()["detail"]
    if err.retry_after:
        assert r.headers["retry-after"] == "30"
    r = client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token))
    assert r.status_code == 200 and len(fake.tickets) == 1


def test_a_timeout_after_jira_made_it_links_that_ticket_not_a_second(client, admin_token, report, fake, db):
    fake.made_then_fail = jira.JiraError(504, "Jira didn't answer in time.", maybe_made=True)
    body = {"issue_type": "Bug", "summary": "The Tonight list"}
    r = client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token))
    assert r.status_code == 504 and "won't" not in r.json()["detail"] and "not made twice" in r.json()["detail"]
    r = client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token))
    assert r.status_code == 409  # held a minute, while Jira's search catches up
    issue = db.get(IssueReport, report["id"])
    issue.jira_pending_at = datetime.now(UTC) - timedelta(minutes=2)
    db.commit()
    r = client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["jira_key"] == "CC-9001" and len(fake.tickets) == 1


def test_concurrent_clicks_make_one_ticket(client, admin_token, report, fake, engine):
    if not IS_POSTGRES:
        pytest.skip("SQLite in tests shares one connection; Postgres checks the race")
    body = {"issue_type": "Bug", "summary": "The Tonight list"}
    try:
        with ThreadPoolExecutor(4) as pool:
            codes = list(pool.map(lambda _: client.post(f"/admin/issues/{report['id']}/jira", json=body, headers=auth(admin_token)).status_code,
                                  range(4)))
    finally:
        engine.dispose()  # the threads' extra connections would outlive the next test's drop and re-create of the tables
    assert len(fake.tickets) == 1 and set(codes) <= {200, 409} and 200 in codes


def test_not_connected(client, admin_token, report):
    assert get_settings().jira_mode == "none"  # tests and local, unless JIRA_BACKEND is set
    assert draft(client, admin_token, report["id"])["connected"] is False
    r = client.post(f"/admin/issues/{report['id']}/jira", json={"issue_type": "Bug", "summary": "The Tonight list"}, headers=auth(admin_token))
    assert r.status_code == 503 and "isn't connected" in r.json()["detail"]
    r = client.post(f"/admin/issues/{report['id']}/jira", json={"issue_type": "Bug", "summary": "The Tonight list"}, headers=auth(admin_token))
    assert r.status_code == 503  # the claim was let go, not held


def test_bad_input(client, admin_token, report, fake):
    url = f"/admin/issues/{report['id']}/jira"
    assert client.post(url, json={"issue_type": "Epic", "summary": "The Tonight list"}, headers=auth(admin_token)).status_code == 422
    assert client.post(url, json={"issue_type": "Bug", "priority": "Urgent", "summary": "Hi there"}, headers=auth(admin_token)).status_code == 422
    assert client.post(url, json={"issue_type": "Bug", "summary": "  "}, headers=auth(admin_token)).status_code == 422
    assert client.post("/admin/issues/999/jira", json={"issue_type": "Bug", "summary": "Hi there"}, headers=auth(admin_token)).status_code == 404
    assert fake.calls == []


# ---- the real adapter, against a stubbed Jira -------------------------------------------

class _Res(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _http_error(code, body=b"{}", headers=None):
    return urllib.error.HTTPError("https://x", code, "x", headers or {}, io.BytesIO(body))


def test_api_sends_auth_and_reads_answers(monkeypatch):
    sent = []

    def urlopen(req, timeout):
        sent.append(req)
        if req.get_method() == "POST":
            return _Res(b'{"id": "1", "key": "CC-300"}')
        return _Res(json.dumps({"issues": [{"key": "CC-300", "fields": {"status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                                                                    "labels": ["member-feedback", "report-ISS-0001"]}}]}).encode())
    monkeypatch.setattr(jira.urllib.request, "urlopen", urlopen)
    api = jira.JiraApi("someone@example.org:tok")
    assert api.create("Bug", "Medium", "Summary", "One\nTwo\n\nThree", ["member-feedback", "report-ISS-0001"]) == "CC-300"
    body = json.loads(sent[0].data)["fields"]
    assert sent[0].full_url == "https://api.atlassian.com/ex/jira/afcbf044-3608-4c7e-b276-ba503e355435/rest/api/3/issue" and sent[0].headers["Authorization"].startswith("Basic ")
    assert body["project"] == {"key": "CC"} and body["issuetype"] == {"name": "Bug"} and body["priority"] == {"name": "Medium"}
    assert body["description"]["content"][0]["content"] == [{"type": "text", "text": "One"}, {"type": "hardBreak"}, {"type": "text", "text": "Two"}]
    [t] = api.find(["report-ISS-0001"])
    assert (t.key, t.status, t.category) == ("CC-300", "In Progress", "indeterminate")
    assert "search/jql" in sent[1].full_url and "report-ISS-0001" in sent[1].full_url


@pytest.mark.parametrize("raised,status,maybe", [
    (_http_error(401), 502, False), (_http_error(403), 502, False), (_http_error(429, headers={"Retry-After": "12"}), 503, False),
    (_http_error(400, b'{"errorMessages": [], "errors": {"priority": "Priority name is not valid"}}'), 422, False),
    (_http_error(503), 502, True), (TimeoutError("slow"), 504, True), (urllib.error.URLError("down"), 504, True)])
def test_api_failures(monkeypatch, raised, status, maybe):
    def urlopen(req, timeout):
        raise raised
    monkeypatch.setattr(jira.urllib.request, "urlopen", urlopen)
    with pytest.raises(jira.JiraError) as e:
        jira.JiraApi("someone@example.org:tok").create("Bug", "Medium", "S", "D", [])
    assert (e.value.status, e.value.maybe_made) == (status, maybe)
    if status == 503:
        assert e.value.retry_after == 12
    if status == 422:
        assert "Priority name is not valid" in e.value.message


def test_credentials_come_from_parameter_store_in_production(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "jira_backend", "api")
    monkeypatch.setattr(jira, "_ssm_credentials", lambda _: "unset")
    with pytest.raises(jira.JiraError) as e:
        jira.client()
    assert e.value.status == 503 and "no Jira API token" in e.value.message
    monkeypatch.setattr(jira, "_ssm_credentials", lambda _: "someone@example.org:tok")
    assert isinstance(jira.client(), jira.JiraApi)

    def broken(_):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(jira, "_ssm_credentials", broken)
    with pytest.raises(jira.JiraError):
        jira.client()


def test_log_lines_carry_no_report_text(client, admin_token, report, fake, caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        client.post(f"/admin/issues/{report['id']}/jira", json={"issue_type": "Bug", "summary": "The Tonight list was empty"},
                    headers=auth(admin_token))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("jira:")]
    assert lines and "ISS-0001 made CC-9001" in lines[0] and not any("Tonight" in x for x in lines)
