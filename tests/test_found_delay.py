"""CC-257: delay a found event for 1 week, 1 month or 2 months; it comes back as new on its day."""
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.discovery.readers import FoundEvent
from app.discovery.store import record, release_delayed
from app.models import DiscoveredItem
from app.routers.discovery import _back_on
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import make_source


def one(db, title="Hamlet"):
    s = make_source(db)
    record(db, s, FoundEvent(title=title, url="", kind="production", category="Theater", begin_date="2027-03-10"), "")
    db.commit()
    return db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == title))


@pytest.mark.parametrize("today,span,want", [
    (date(2026, 11, 16), "1w", date(2026, 11, 23)),
    (date(2026, 11, 18), "1m", date(2026, 12, 18)),
    (date(2026, 11, 18), "2m", date(2027, 1, 18)),
    (date(2026, 12, 31), "1m", date(2027, 1, 31)),
    (date(2026, 12, 31), "2m", date(2027, 2, 28)),
    (date(2026, 1, 31), "1m", date(2026, 2, 28)),
])
def test_back_on_dates(today, span, want):
    assert _back_on(today, span) == want


def test_delay_hides_it_with_its_return_day_and_changes_nothing_else(client, db, admin_token):
    item = one(db)
    r = client.post(f"/admin/discovery/items/{item.id}/delay", json={"span": "1m"}, headers=auth(admin_token))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "hidden" and body["back_on"] == _back_on(today_in_chicago(), "1m").isoformat()
    assert body["title"] == "Hamlet" and body["begin_date"] == "2027-03-10"
    waiting = client.get("/admin/discovery/items", headers=auth(admin_token)).json()
    assert waiting["total"] == 0
    hidden = client.get("/admin/discovery/items?show=hidden", headers=auth(admin_token)).json()
    assert [i["back_on"] for i in hidden["items"]] == [body["back_on"]]
    assert client.get("/admin/discovery/summary", headers=auth(admin_token)).json()["hidden"] == 1


def test_it_returns_as_new_once_the_day_arrives(client, db, admin_token):
    item = one(db)
    client.post(f"/admin/discovery/items/{item.id}/delay", json={"span": "1w"}, headers=auth(admin_token))
    db.refresh(item)
    assert release_delayed(db, item.delayed_until - timedelta(seconds=1)) == 0 and item.status == "hidden"
    item.delayed_until = datetime.now(UTC) - timedelta(minutes=1)  # the day has come
    db.commit()
    r = client.get("/admin/discovery/items", headers=auth(admin_token)).json()
    assert [(i["title"], i["status"], i["back_on"]) for i in r["items"]] == [("Hamlet", "new", None)]
    db.refresh(item)
    assert item.delayed_until is None


def test_unhide_brings_it_back_early_and_clears_the_delay(client, db, admin_token):
    item = one(db)
    client.post(f"/admin/discovery/items/{item.id}/delay", json={"span": "2m"}, headers=auth(admin_token))
    r = client.post(f"/admin/discovery/items/{item.id}/unhide", headers=auth(admin_token)).json()
    assert r["status"] == "new" and r["back_on"] is None
    db.refresh(item)
    assert item.delayed_until is None


def test_only_new_or_hidden_ones_can_be_delayed_and_only_by_admins(client, db, admin_token, user_token):
    item = one(db)
    assert client.post(f"/admin/discovery/items/{item.id}/delay", json={"span": "1w"}, headers=auth(user_token)).status_code == 403
    assert client.post(f"/admin/discovery/items/{item.id}/delay", json={"span": "3d"}, headers=auth(admin_token)).status_code == 422
    assert client.post("/admin/discovery/items/99999/delay", json={"span": "1w"}, headers=auth(admin_token)).status_code == 404
    db.execute(update(DiscoveredItem).where(DiscoveredItem.id == item.id).values(status="rejected"))
    db.commit()
    assert client.post(f"/admin/discovery/items/{item.id}/delay", json={"span": "1w"}, headers=auth(admin_token)).status_code == 409


def test_one_that_ran_out_while_delayed_is_rejected_as_passed_not_returned(client, db, admin_token):
    s = make_source(db)
    record(db, s, FoundEvent(title="Gone", url="", kind="production", category="Theater", begin_date="2026-01-01", end_date="2026-01-31"), "")
    db.commit()
    item = db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == "Gone"))
    item.status, item.delayed_until = "hidden", datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    assert release_delayed(db, datetime.now(UTC)) == 1
    db.refresh(item)
    assert item.status == "rejected" and item.delayed_until is None
