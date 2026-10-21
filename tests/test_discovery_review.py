"""CC-71: reviewing found events: approve, reject (and skip similar), accept or dismiss updates, CSV."""
import csv
import io
from datetime import date

from sqlalchemy import select

from app.discovery.readers import FoundEvent
from app.discovery.store import record
from app.models import DiscoveredItem, Event, EventStatus, SkipRule, Venue

from .conftest import auth
from .test_discovery import make_source


def found(title, url="", **kw):
    return FoundEvent(title=title, url=url, kind=kw.pop("kind", "production"), category=kw.pop("category", "Theater"), **kw)


def item_named(db, title):
    return db.scalar(select(DiscoveredItem).where(DiscoveredItem.title == title))


def test_the_list_shows_whats_waiting_with_venue_duplicates_and_unreadable_sources(client, admin_token, user_token, db, make_event):
    s = make_source(db)
    make_event("Hamlet: A Tragedy", venue_name="Goodman Theatre", neighborhood="Loop")
    record(db, s, found("Hamlet", "https://goodman.example/hamlet", begin_date="2027-01-10", cost_text="$25-$90"), "")
    record(db, s, found("Wheaton Gala Concert", venue_name="Wheaton College Hall", kind="concert"), "")
    record(db, s, found("Acting 101", kind="class"), "a class or workshop")
    s.last_status, s.last_error = "blocked", "the site refused the request (HTTP 403)"
    db.commit()
    h = auth(admin_token)
    r = client.get("/admin/discovery/items", headers=h).json()
    assert r["total"] == 2 and r["counts"] == {"new": 2, "skipped": 1, "needs": 1}  # the concert has no date (CC-88)
    by = {i["title"]: i for i in r["items"]}
    hamlet = by["Hamlet"]
    assert hamlet["source_name"] == "Goodman" and hamlet["venue_name"] == "Goodman Theatre" and hamlet["neighborhood"] == "Loop"
    assert hamlet["venue_id"] is not None and hamlet["possible_duplicate"]["title"] == "Hamlet: A Tragedy"
    assert by["Wheaton Gala Concert"]["venue_id"] is None  # an unknown venue: the admin picks or adds it
    assert by["Wheaton Gala Concert"]["neighborhood"] == ""  # not the source's own, so no guess
    assert r["unreadable"] == [{"id": s.id, "name": "Goodman", "status": "blocked", "reason": "the site refused the request (HTTP 403)",
                                "last_checked_at": None}]
    assert [i["title"] for i in client.get("/admin/discovery/items?show=skipped", headers=h).json()["items"]] == ["Acting 101"]
    assert client.get("/admin/discovery/items", headers=auth(user_token)).status_code == 403


def test_approve_adds_it_to_the_catalog_and_remembers_the_source(client, admin_token, db):
    s = make_source(db)
    record(db, s, found("Hamlet", "https://goodman.example/hamlet"), ""); db.commit()
    item = item_named(db, "Hamlet")
    body = {"title": "Hamlet", "category": "Theater", "venue_id": s.venue_id, "begin_date": "2027-01-10",
            "end_date": "2027-02-14", "cost_text": "$25", "min_price": 25, "url": item.url, "publish": "now"}
    r = client.post(f"/admin/discovery/items/{item.id}/approve", json=body, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["status"] == "approved"
    event = db.get(Event, r.json()["event"]["id"])
    assert event.status == EventStatus.approved and event.found_by_id == s.id and event.title == "Hamlet"
    assert client.post(f"/admin/discovery/items/{item.id}/approve", json=body, headers=auth(admin_token)).status_code == 409
    # a new venue named in the form is added
    record(db, s, found("Gala", "https://goodman.example/gala"), ""); db.commit()
    gala = item_named(db, "Gala")
    body = {**body, "title": "Gala", "url": gala.url, "venue_id": None, "venue": {"name": "Wheaton Hall", "neighborhood": "Wheaton"},
            "publish": "review", "weekly": {"sat": ["19:30"]}}  # a new venue has no usual show times, so they're given (CC-88)
    r = client.post(f"/admin/discovery/items/{gala.id}/approve", json=body, headers=auth(admin_token))
    assert r.status_code == 200 and db.get(Event, r.json()["event"]["id"]).status == EventStatus.pending
    assert db.scalar(select(Venue).where(Venue.name == "Wheaton Hall")) is not None


def test_reject_and_skip_similar_catches_waiting_and_future_items(client, admin_token, db):
    s = make_source(db)
    for t in ("Student Jam", "Improv Student Jam Night", "Hamlet", "Jamboree"):
        record(db, s, found(t), "")
    db.commit()
    h = auth(admin_token)
    jam = item_named(db, "Student Jam")
    r = client.post(f"/admin/discovery/items/{jam.id}/reject", json={"skip_similar": {"phrase": "Student jam!"}}, headers=h)
    assert r.status_code == 200 and r.json()["also_rejected"] == 1 and r.json()["item"]["status"] == "rejected"
    db.expire_all()
    assert item_named(db, "Improv Student Jam Night").status == "rejected"
    assert item_named(db, "Jamboree").status == "new" and item_named(db, "Hamlet").status == "new"  # whole words only
    # a future one is rejected quietly, and the rule counts it
    assert record(db, s, found("Saturday Student Jam"), "") == "skipped"
    db.commit()
    rules = client.get("/admin/discovery/rules", headers=h).json()
    assert [(x["phrase"], x["source_name"], x["caught"]) for x in rules] == [("student jam", "Goodman", 3)]
    # plain reject makes no rule; deleting the rule stops it
    hamlet = item_named(db, "Hamlet")
    assert client.post(f"/admin/discovery/items/{hamlet.id}/reject", json={}, headers=h).json()["also_rejected"] == 0
    assert client.delete(f"/admin/discovery/rules/{rules[0]['id']}", headers=h).status_code == 204
    assert db.scalar(select(SkipRule)) is None
    assert record(db, s, found("Sunday Student Jam"), "") == "new"


def test_accept_or_dismiss_a_proposed_update(client, admin_token, db, make_event):
    s = make_source(db)
    listed = make_event("Hamlet", venue_name="Goodman Theatre", neighborhood="Loop", begin=date(2027, 1, 10), end=date(2027, 2, 14))
    listed.cost_text = ""; db.commit()
    record(db, s, found("Hamlet", begin_date="2027-01-10", end_date="2027-02-21", cost_text="$30-$80", min_price=30), "")
    db.commit()
    h = auth(admin_token)
    item = item_named(db, "Hamlet")
    r = client.get("/admin/discovery/items?show=update", headers=h).json()["items"][0]
    assert {c["field"]: (c["listed"], c["found"]) for c in r["proposed"]} == {
        "cost_text": ("", "$30-$80"), "min_price": (None, 30.0), "end_date": ("2027-02-14", "2027-02-21")}
    assert client.post(f"/admin/discovery/items/{item.id}/accept", headers=h).json()["status"] == "listed"
    db.expire_all()  # the API changed them in its own session
    assert (listed.cost_text, float(listed.min_price), listed.end_date) == ("$30-$80", 30.0, date(2027, 2, 21))
    # a change dismissed isn't proposed again; a different one is
    record(db, s, found("Hamlet", begin_date="2027-01-12", end_date="2027-02-21", cost_text="$30-$80", min_price=30), ""); db.commit()
    db.refresh(item)
    assert item.status == "update"
    assert client.post(f"/admin/discovery/items/{item.id}/dismiss", headers=h).json()["status"] == "listed"
    db.expire_all()
    assert record(db, s, found("Hamlet", begin_date="2027-01-12", end_date="2027-02-21", cost_text="$30-$80", min_price=30), "") == "seen"
    assert record(db, s, found("Hamlet", begin_date="2027-01-12", end_date="2027-02-28", cost_text="$30-$80", min_price=30), "") == "update"
    db.refresh(listed)
    assert listed.begin_date == date(2027, 1, 10)  # dismissing changed nothing
    assert client.post(f"/admin/discovery/items/{item.id}/reject", json={}, headers=h).status_code == 409


def test_csv_download(client, admin_token, db):
    s = make_source(db)
    record(db, s, found("Hamlet, the \"Musical\"", "https://goodman.example/hamlet", begin_date="2027-01-10", cost_text="$25"), "")
    db.commit()
    r = client.get("/admin/discovery/items.csv", headers=auth(admin_token))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "Title,Venue,Neighborhood,Opens,Closes,Open run,Price,Link,Source,Found,Status"
    assert lines[1].startswith('"Hamlet, the ""Musical""",Goodman Theatre,Loop,2027-01-10,,,$25,https://goodman.example/hamlet,Goodman,')
    assert lines[1].endswith(",new") and len(lines) == 2


def test_filter_by_price_and_dates(client, admin_token, db):
    """CC-82: priced or not, dated or not; counts, paging and the CSV follow the filters."""
    s = make_source(db)
    record(db, s, found("Priced and dated", cost_text="$30", min_price=30, begin_date="2027-01-10"), "")
    record(db, s, found("Free, no dates", cost_text="Free", min_price=0), "")
    record(db, s, found("Dated only", end_date="2027-03-01"), "")
    record(db, s, found("Neither"), "")
    record(db, s, found("Skipped but priced", kind="class", cost_text="$10"), "a class or workshop")
    db.commit()
    h = auth(admin_token)

    def titles(**params):
        r = client.get("/admin/discovery/items", params=params, headers=h).json()
        return sorted(i["title"] for i in r["items"]), r["total"], r["counts"]

    assert titles(price="listed")[:2] == (["Free, no dates", "Priced and dated"], 2)
    assert titles(price="none")[0] == ["Dated only", "Neither"]
    assert titles(dates="listed")[0] == ["Dated only", "Priced and dated"]
    assert titles(dates="none", price="none")[0] == ["Neither"]
    assert titles(price="listed")[2] == {"new": 2, "skipped": 1, "needs": 1}  # counts per status under the same filters
    assert titles(price="listed", limit=1)[1] == 2
    assert client.get("/admin/discovery/items", params={"price": "maybe"}, headers=h).status_code == 422
    rows = list(csv.reader(io.StringIO(client.get("/admin/discovery/items.csv", params={"dates": "none"}, headers=h).text)))
    assert sorted(r[0] for r in rows[1:]) == ["Free, no dates", "Neither"]
