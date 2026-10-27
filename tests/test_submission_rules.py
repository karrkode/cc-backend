"""CC-100: auto-reject rules for member submissions."""
from datetime import timedelta

from app.stats import today_in_chicago

from .conftest import auth, register


def _submit(client, token, title="New Play", venue="Tiny Stage", hood="Oak Park, IL", venue_id=None):
    start = today_in_chicago() + timedelta(days=10)
    body = {"title": title, "category": "Theater", "begin_date": str(start), "end_date": str(start + timedelta(days=20)),
            "url": "https://tiny.example/play"}
    body.update({"venue_id": venue_id} if venue_id else {"venue": {"name": venue, "neighborhood": hood}})
    r = client.post("/events", json=body, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _rule(client, admin_token, **body):
    r = client.post("/admin/submission-rules", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    return r.json()


def _queue(client, admin_token, query=""):
    return [e["title"] for e in client.get("/admin/events" + query, headers=auth(admin_token)).json()["items"]]


def _rules(client, admin_token):
    return {r["id"]: r for r in client.get("/admin/submission-rules", headers=auth(admin_token)).json()}


def test_name_rule_ignores_case_and_punctuation(client, admin_token, user_token):
    rule = _rule(client, admin_token, kind="name", phrase="Paint & Sip!")
    assert rule["phrase"] == "paint sip" and rule["caught"] == 0 and rule["venue_name"] is None

    caught = _submit(client, user_token, title="PAINT-&-SIP night: Van Gogh")
    assert caught["status"] == "rejected"  # the site shows the usual thanks for anything not approved
    kept = _submit(client, user_token, title="Paintings of Sipping")  # whole words only
    assert kept["status"] == "pending"

    assert _queue(client, admin_token) == ["Paintings of Sipping"]
    found = client.get("/admin/events?status=rejected&caught=true", headers=auth(admin_token)).json()["items"]
    assert [e["title"] for e in found] == ["PAINT-&-SIP night: Van Gogh"]
    assert found[0]["reject_reason"] == "Auto-reject rule: event name “paint sip”"
    assert found[0]["created_at"]  # the site shows when it was caught
    assert _rules(client, admin_token)[rule["id"]]["caught"] == 1
    assert "reject_reason" not in client.get(f"/events/{caught['id']}", headers=auth(user_token)).json()


def test_venue_rule(client, admin_token, user_token, make_event):
    venue_id = make_event(venue_name="Party Barn", neighborhood="Wicker Park").venue_id
    rule = _rule(client, admin_token, kind="venue", venue_id=venue_id)
    assert rule["venue_name"] == "Party Barn · Wicker Park"

    assert _submit(client, user_token, title="Anything", venue_id=venue_id)["status"] == "rejected"
    # typed in rather than picked: the same venue is reused, so the rule still applies
    assert _submit(client, user_token, title="Something else", venue="party barn", hood="wicker park")["status"] == "rejected"
    assert _submit(client, user_token, title="Elsewhere", venue="Tiny Stage")["status"] == "pending"
    assert _rules(client, admin_token)[rule["id"]]["caught"] == 2
    reasons = {e["reject_reason"] for e in client.get("/admin/events?status=rejected&caught=true", headers=auth(admin_token)).json()["items"]}
    assert reasons == {"Auto-reject rule: venue “Party Barn”"}


def test_no_rules_and_no_match_go_to_review(client, admin_token, user_token):
    assert _submit(client, user_token, title="Hamlet")["status"] == "pending"
    _rule(client, admin_token, kind="name", phrase="bar crawl")
    assert _submit(client, user_token, title="Macbeth")["status"] == "pending"
    assert _queue(client, admin_token) == ["Hamlet", "Macbeth"]
    assert all(r["caught"] == 0 for r in _rules(client, admin_token).values())


def test_admins_own_submissions_are_never_caught(client, admin_token):
    rule = _rule(client, admin_token, kind="name", phrase="bar crawl")
    assert _submit(client, admin_token, title="Bar Crawl")["status"] == "pending"
    assert _rules(client, admin_token)[rule["id"]]["caught"] == 0


def test_approving_a_caught_submission_and_deleting_the_rule(client, admin_token, user_token):
    rule = _rule(client, admin_token, kind="name", phrase="bar crawl")
    ev = _submit(client, user_token, title="Bar Crawl")
    r = client.post(f"/admin/events/{ev['id']}/approve", headers=auth(admin_token))
    assert r.json()["status"] == "approved" and r.json()["reject_reason"] == ""
    assert _queue(client, admin_token, "?status=rejected&caught=true") == []

    assert client.delete(f"/admin/submission-rules/{rule['id']}", headers=auth(admin_token)).status_code == 204
    assert _rules(client, admin_token) == {}
    assert _submit(client, user_token, title="Bar Crawl 2")["status"] == "pending"
    assert client.delete(f"/admin/submission-rules/{rule['id']}", headers=auth(admin_token)).status_code == 404


def test_rule_validation_and_admin_only(client, admin_token, user_token):
    h = auth(admin_token)
    assert client.post("/admin/submission-rules", json={"kind": "name", "phrase": " !! "}, headers=h).status_code == 422
    assert client.post("/admin/submission-rules", json={"kind": "venue"}, headers=h).status_code == 422
    assert client.post("/admin/submission-rules", json={"kind": "venue", "venue_id": 99999}, headers=h).status_code == 422
    assert client.post("/admin/submission-rules", json={"kind": "other", "phrase": "x"}, headers=h).status_code == 422
    _rule(client, admin_token, kind="name", phrase="Bar crawl")
    assert client.post("/admin/submission-rules", json={"kind": "name", "phrase": "BAR CRAWL."}, headers=h).status_code == 409
    other = register(client, email="bo@example.com", name="Bo")
    assert client.get("/admin/submission-rules", headers=auth(other)).status_code == 403
    assert client.post("/admin/submission-rules", json={"kind": "name", "phrase": "x"}, headers=auth(user_token)).status_code == 403


def test_merging_a_venue_keeps_its_rule(client, admin_token, user_token, make_event):
    dupe = make_event(title="A", venue_name="Party Barn", neighborhood="").venue_id
    keep = make_event(title="B", venue_name="The Party Barn", neighborhood="Wicker Park").venue_id
    _rule(client, admin_token, kind="venue", venue_id=dupe)
    assert client.post(f"/venues/{dupe}/merge-into/{keep}", headers=auth(admin_token)).status_code == 200
    [rule] = _rules(client, admin_token).values()
    assert rule["venue_id"] == keep
    assert _submit(client, user_token, title="C", venue_id=keep)["status"] == "rejected"
