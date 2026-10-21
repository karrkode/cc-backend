"""CC-91: approvers. Only the admin offers the permission, the member accepts it, and approvers decide on
"I've been" photos. The admin (info@ in production) can always approve."""
import pytest
from sqlalchemy import select

from app import photos
from app.config import get_settings
from app.discovery.recap import build_recap
from app.models import User, WentPhoto

from .conftest import auth, register
from .test_went_photos import JPEG, been, files, upload


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "photo_dir", str(tmp_path))
    return tmp_path


def user_id(db, email):
    return str(db.scalar(select(User.id).where(User.email == email)))


def test_only_the_admin_offers_and_the_member_accepts(client, admin_token, user_token, db):
    other = register(client, email="sam@example.com", name="Sam Okafor")
    me = client.get("/auth/me", headers=auth(user_token)).json()
    assert (me["can_approve"], me["approver_offered"]) == (False, False)
    assert client.get("/auth/me", headers=auth(admin_token)).json()["can_approve"]  # admins always can

    sam = user_id(db, "sam@example.com")
    assert client.post(f"/admin/members/{sam}/approver", headers=auth(user_token)).status_code == 403
    r = client.post(f"/admin/members/{sam}/approver", headers=auth(admin_token))
    assert r.status_code == 200 and (r.json()["approver"], r.json()["approver_offered"]) == (False, True)
    assert client.get("/approvals", headers=auth(other)).status_code == 403  # not until they accept

    mine = client.get("/auth/me", headers=auth(other)).json()
    assert mine["approver_offered"] and not mine["can_approve"]
    r = client.post("/me/approver", json={"accept": True}, headers=auth(other))
    assert r.status_code == 200 and r.json()["can_approve"] and not r.json()["approver_offered"]
    assert client.get("/approvals", headers=auth(other)).status_code == 200
    assert client.post("/me/approver", json={"accept": True}, headers=auth(other)).status_code == 404  # nothing waiting now

    listed = client.get("/admin/members?show=approvers", headers=auth(admin_token)).json()
    assert [(m["display_name"], m["approver"]) for m in listed] == [("Sam Okafor", True)]
    assert client.post(f"/admin/members/{user_id(db, 'admin@example.com')}/approver", headers=auth(admin_token)).status_code == 409

    # removed: at once
    assert client.delete(f"/admin/members/{sam}/approver", headers=auth(admin_token)).json()["approver"] is False
    assert client.get("/approvals", headers=auth(other)).status_code == 403

    # declining clears the offer
    client.post(f"/admin/members/{sam}/approver", headers=auth(admin_token))
    assert client.post("/me/approver", json={"accept": False}, headers=auth(other)).json()["approver_offered"] is False
    assert client.get("/approvals", headers=auth(other)).status_code == 403


def test_approvers_approve_or_reject_photos(client, admin_token, user_token, db, make_event, stored):
    ev = make_event("Hamlet")
    client.patch("/auth/me", json={"display_name": "Jordan Rivera"}, headers=auth(user_token))
    been(client, user_token, ev.id)
    upload(client, user_token, ev.id)
    db.scalar(select(WentPhoto)).blurred = True
    db.scalar(select(WentPhoto)).labels = [{"name": "Suggestive", "parent": "", "confidence": 62.0}]
    db.commit()

    page = client.get("/approvals", headers=auth(admin_token)).json()
    item = page["items"][0]
    assert page["counts"] == {"waiting": 1}
    assert (item["event"]["title"], item["venue"], item["member"], item["blurred"]) == ("Hamlet", "Goodman Theatre", "Jordan R.", True)
    assert item["labels"][0]["name"] == "Suggestive" and client.get(item["url"]).content == JPEG

    assert client.post(f"/approvals/{item['id']}", json={"decision": "approve"}, headers=auth(user_token)).status_code == 403
    r = client.post(f"/approvals/{item['id']}", json={"decision": "approve"}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.post(f"/approvals/{item['id']}", json={"decision": "reject"}, headers=auth(admin_token)).status_code == 409
    assert client.get("/me/attended", headers=auth(user_token)).json()[0]["photo"]["status"] == "approved"
    assert client.get("/approvals?show=approved", headers=auth(admin_token)).json()["items"][0]["id"] == item["id"]

    # a new photo waits again; rejecting deletes the file, and the member sees why
    upload(client, user_token, ev.id)
    new = client.get("/approvals", headers=auth(admin_token)).json()["items"][0]
    r = client.post(f"/approvals/{new['id']}", json={"decision": "reject", "reason": "  That's a different show.  "}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["url"] == "" and files(stored) == []
    mine = client.get("/me/attended", headers=auth(user_token)).json()[0]["photo"]
    assert (mine["status"], mine["reason"], mine["url"]) == ("rejected", "That's a different show.", "")


def test_nobody_approves_their_own_photo(client, admin_token, db, make_event, stored):
    ev = make_event("Hamlet")
    been(client, admin_token, ev.id)
    upload(client, admin_token, ev.id)
    assert client.get("/approvals", headers=auth(admin_token)).json() == {"items": [], "counts": {}}
    pid = db.scalar(select(WentPhoto.id))
    r = client.post(f"/approvals/{pid}", json={"decision": "approve"}, headers=auth(admin_token))
    assert r.status_code == 403 and "Someone else" in r.json()["detail"]


def test_the_recap_counts_photos_waiting(client, user_token, make_event, db, stored):
    ev = make_event("Hamlet")
    been(client, user_token, ev.id)
    upload(client, user_token, ev.id)
    email = build_recap(db)
    assert "PHOTOS WAITING FOR APPROVAL: 1" in email.text and "#approvals" in email.text and "1 photo waiting" in email.html
