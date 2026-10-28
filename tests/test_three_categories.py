"""CC-133: Comedy and Educational go. Three categories, the withdrawn events' review, deleting a discovery source,
and discovery leaving out events in a removed category."""
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text

from app.db import Base
from app.discovery.store import record
from app.models import DiscoveredItem, DiscoveryCheck, DiscoverySource, EventStatus, SkipRule, SourceKind

from .conftest import IS_POSTGRES, TEST_DATABASE_URL, auth
from .test_discovery import make_source
from .test_discovery_review import found, item_named


def test_the_api_refuses_comedy_and_educational(client, user_token, admin_token, make_event):
    ev = make_event("Hamlet")
    for category in ("Comedy", "Educational"):
        body = {"title": "Set", "category": category, "venue_id": ev.venue_id}
        assert client.post("/events", json=body, headers=auth(user_token)).status_code == 422
        assert client.patch(f"/events/{ev.id}", json={"category": category}, headers=auth(admin_token)).status_code == 422
        assert client.patch(f"/venues/{ev.venue_id}", json={"category": category}, headers=auth(admin_token)).status_code == 422
    assert client.get("/events/facets").json()["categories"] == ["Theater", "Music", "Visual Art"]


def withdrawn(make_event, db, title, reason, **kw):
    ev = make_event(title, status=EventStatus.withdrawn, **kw)
    ev.withdrawn_reason = reason; db.commit()
    return ev


def test_the_review_lists_whats_withdrawn_and_brings_one_back_with_a_category(client, admin_token, user_token, make_event, db):
    musical = withdrawn(make_event, db, "Improvised Musical", "Comedy", venue_name="iO", category="Comedy")
    withdrawn(make_event, db, "Mainstage Revue", "The Second City", venue_name="The Second City", category="Theater")
    make_event("Old sheet show", status=EventStatus.withdrawn)  # withdrawn before, for another reason: not on the list
    h = auth(admin_token)
    r = client.get("/admin/events/removed", headers=h)
    assert [(e["title"], e["withdrawn_reason"]) for e in r.json()] == [("Improvised Musical", "Comedy"),
                                                                       ("Mainstage Revue", "The Second City")]
    assert client.get("/admin/events/removed", headers=auth(user_token)).status_code == 403
    assert client.get("/admin/events/other-types", headers=h).json() == []  # not on CC-125's list as well

    # still Comedy: it can't go back on show, by either route
    assert client.patch(f"/events/{musical.id}", json={"status": "approved"}, headers=h).status_code == 422
    assert client.post(f"/admin/events/{musical.id}/approve", headers=h).status_code == 422
    assert client.get(f"/events/{musical.id}").status_code == 404
    # with Theater it can
    r = client.patch(f"/events/{musical.id}", json={"status": "approved", "category": "Theater"}, headers=h)
    assert r.status_code == 200 and r.json()["category"] == "Theater"
    assert client.get(f"/events/{musical.id}").status_code == 200
    assert [e["title"] for e in client.get("/admin/events/removed", headers=h).json()] == ["Mainstage Revue"]
    # a Theater event at The Second City comes back as it is
    revue = next(e for e in client.get("/admin/events/removed", headers=h).json())
    assert client.patch(f"/events/{revue['id']}", json={"status": "approved"}, headers=h).status_code == 200
    assert client.get("/admin/events/removed", headers=h).json() == []


def test_a_withdrawn_event_stays_in_the_members_journal(client, user_token, make_event, db):
    ev = make_event("Mainstage Revue", venue_name="The Second City")
    assert client.put(f"/me/attended/{ev.id}", headers=auth(user_token)).status_code in (200, 201)
    ev.status, ev.withdrawn_reason = EventStatus.withdrawn, "The Second City"; db.commit()
    assert "Mainstage Revue" not in [e["title"] for e in client.get("/events").json()["items"]]
    assert client.get(f"/events/{ev.id}").status_code == 404
    assert [a["event"]["title"] for a in client.get("/me/attended", headers=auth(user_token)).json()] == ["Mainstage Revue"]


def test_deleting_a_source_keeps_what_it_found_and_its_checks(client, admin_token, user_token, db):
    s = make_source(db, active=True)
    record(db, s, found("Hamlet", "https://goodman.example/hamlet"), "")
    record(db, s, found("Bug", "https://goodman.example/bug"), "")
    db.commit()
    item_named(db, "Bug").status = "rejected"
    db.add(DiscoveryCheck(source_id=s.id, what="source", status="ok", pages_read=1, input_tokens=1000, output_tokens=100))
    db.add(SkipRule(phrase="gala", source_id=s.id, caught=0))
    db.add(SkipRule(phrase="brunch", source_id=None, caught=0))
    db.commit()
    sid, vid = s.id, s.venue_id

    assert client.delete(f"/admin/discovery/sources/{sid}", headers=auth(user_token)).status_code == 403
    assert client.delete(f"/admin/discovery/sources/{sid}", headers=auth(admin_token)).status_code == 204
    assert client.delete(f"/admin/discovery/sources/{sid}", headers=auth(admin_token)).status_code == 404
    db.expire_all()
    assert db.get(DiscoverySource, sid) is None
    assert {(i.title, i.status, i.source_id) for i in db.scalars(select(DiscoveredItem))} == {("Hamlet", "new", None),
                                                                                           ("Bug", "rejected", None)}
    assert db.scalar(select(func.count()).select_from(DiscoveryCheck)) == 1  # still counts toward the month's spend
    assert [r.phrase for r in db.scalars(select(SkipRule))] == ["brunch"]  # its own rule went with it
    overview = client.get("/admin/discovery/sources", headers=auth(admin_token)).json()
    assert overview["sources"] == [] and overview["cost_30d_usd"] > 0
    # its waiting event still shows for review, and can be approved
    listed = client.get("/admin/discovery/items", headers=auth(admin_token)).json()["items"]
    assert [(i["title"], i["source_name"], i["source_id"]) for i in listed] == [("Hamlet", "Deleted source", None)]
    assert (listed[0]["venue_id"], listed[0]["venue_name"], listed[0]["neighborhood"]) == (vid, "Goodman Theatre", "Loop")
    hamlet = item_named(db, "Hamlet")
    body = {"phrase": "hamlet", "everywhere": False}
    assert client.post(f"/admin/discovery/items/{hamlet.id}/reject", json={"skip_similar": body},
                       headers=auth(admin_token)).status_code == 422  # a rule for no source would be one for all


def test_discovery_leaves_out_comedy_and_educational(db):
    s = make_source(db)
    record(db, s, found("Stand-up night", "https://goodman.example/standup", kind="comedy_show", category="Comedy"), "")
    record(db, s, found("Curator talk", "https://goodman.example/talk", kind="talk", category="Educational"), "")
    record(db, s, found("Hamlet", "https://goodman.example/hamlet"), "")
    club = DiscoverySource(name="Comedy club", kind=SourceKind.comedy, pages=[], reader="claude", venue_name="Club",
                           neighborhood="Uptown", every_days=14, page_state={}, venue_hours={}, unchanged_streak=0,
                           last_status="", last_error="")
    db.add(club); db.flush()
    record(db, club, found("Late show", "https://club.example/late", kind="comedy_show", category=None), "")
    db.commit()
    got = {i.title: (i.status, i.skip_reason) for i in db.scalars(select(DiscoveredItem))}
    assert got == {"Stand-up night": ("skipped", "Comedy, which isn't listed"),
                   "Curator talk": ("skipped", "Educational, which isn't listed"),
                   "Hamlet": ("new", ""),
                   "Late show": ("skipped", "Comedy, which isn't listed")}  # a comedy source's events are Comedy


@pytest.mark.skipif(not IS_POSTGRES, reason="migrations target Postgres; set TEST_DATABASE_URL to run")
def test_cc133_migration_withdraws_by_category_and_venue(engine):
    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for (t,) in conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).all():
            conn.execute(text(f'DROP TYPE IF EXISTS "{t}" CASCADE'))
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "0035")
    try:
        with create_engine(TEST_DATABASE_URL).begin() as conn:
            conn.execute(text("INSERT INTO venues (id, name, neighborhood, area, category) VALUES "
                              "(1, 'The Second City', 'Old Town', 'city', 'Comedy'), (2, 'Annoyance Theatre', 'Uptown', 'city', 'Theater'), "
                              "(3, 'Goodman', 'Loop', 'city', 'Theater'), (4, 'iO', 'Lincoln Park', 'city', 'Comedy'), "
                              "(5, 'Art Institute', 'Loop', 'city', 'Visual Art')"))
            rows = [("revue", 1, "Theater", "approved"), ("improv", 2, "Comedy", "approved"), ("hamlet", 3, "Theater", "approved"),
                    ("standup", 4, "Comedy", "approved"), ("lecture", 3, "Educational", "pending"), ("talk", 5, "Visual Art", "approved"),
                    ("old", 4, "Comedy", "rejected"), ("gone", 1, "Theater", "withdrawn")]
            for i, (t, v, cat, st) in enumerate(rows):
                conn.execute(text("INSERT INTO events (id, title, sort_title, venue_id, category, status, open_run, cost_text, url) "
                                  "VALUES (:i, :t, :t, :v, :c, :s, false, '', '')"), {"i": i + 1, "t": t, "v": v, "c": cat, "s": st})
            sc = conn.execute(text("SELECT id FROM discovery_sources WHERE name = 'The Second City'")).scalar()
            for i, (t, cat, st) in enumerate([("set", "Comedy", "new"), ("play", "Theater", "new"), ("done", "Comedy", "approved")]):
                conn.execute(text("INSERT INTO discovered_items (source_id, key, status, skip_reason, title, url, kind, category, "
                                  "venue_name, open_run, cost_text, performances, changes, detail_checks) VALUES "
                                  "(:s, :k, :st, '', :t, '', 'production', :c, '', false, '', '[]', '{}', '[]')"),
                             {"s": sc, "k": f"title:{t}", "st": st, "t": t, "c": cat})
        command.upgrade(cfg, "0036")
        with create_engine(TEST_DATABASE_URL).begin() as conn:
            events = {t: (s, r) for t, s, r in conn.execute(text("SELECT title, status, withdrawn_reason FROM events")).all()}
            defaults = dict(conn.execute(text("SELECT name, category FROM venues")).all())
            items = {t: (s, r) for t, s, r in conn.execute(text("SELECT title, status, skip_reason FROM discovered_items")).all()}
            # deleting a source now keeps what it found
            conn.execute(text("DELETE FROM discovery_sources WHERE id = :s"), {"s": sc})
            kept = conn.execute(text("SELECT count(*) FROM discovered_items WHERE source_id IS NULL")).scalar()
        assert events == {"revue": ("withdrawn", "The Second City"), "improv": ("withdrawn", "Comedy"),
                          "hamlet": ("approved", ""), "standup": ("withdrawn", "Comedy"), "lecture": ("withdrawn", "Educational"),
                          "talk": ("approved", ""), "old": ("rejected", ""), "gone": ("withdrawn", "")}
        assert defaults == {"The Second City": None, "Annoyance Theatre": None, "Goodman": "Theater", "iO": None,
                            "Art Institute": "Visual Art"}
        assert items == {"set": ("skipped", "Comedy, which isn't listed"), "play": ("new", ""), "done": ("approved", "")}
        assert kept == 3
        command.downgrade(cfg, "0035")
    finally:
        command.downgrade(cfg, "base")
