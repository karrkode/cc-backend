"""CC-221: a source for every venue with a website, and the numbers behind the Source grid."""
from datetime import timedelta

from sqlalchemy import select

from app.discovery import run as runner
from app.discovery.fetch import Blocked
from app.discovery.venuesources import FIRST_READS_PER_DAY, add_missing, calendar_link, size_of, state_of
from app.models import Area, DiscoveredItem, DiscoverySource, Event, EventStatus, SourceKind, Venue
from app.stats import today_in_chicago

from .conftest import auth
from .test_discovery import FakeClaude, TODAY, make_source, pages  # noqa: F401 - pages is a fixture


def venue(db, name, website="", **kw):
    v = Venue(name=name, neighborhood=kw.pop("neighborhood", "Loop"), website=website, **kw)
    db.add(v); db.commit()
    return v


def sources(db):
    return db.scalars(select(DiscoverySource).order_by(DiscoverySource.id)).all()


def event(db, v, begin, **kw):
    db.add(Event(title=kw.pop("title", f"Show {begin}"), category="Theater", venue_id=v.id, begin_date=begin,
                 status=kw.pop("status", EventStatus.approved), **kw))
    db.commit()


def home(*links):
    return {"html": "<html><body><nav>" + "".join(f'<a href="{h}">{t}</a>' for h, t in links) + "</nav></body></html>"}


# ---- adding the venues ---------------------------------------------------------------

def test_a_venue_with_a_website_gets_a_source(db):
    v = venue(db, "Hyde Park Art Center", "https://www.hydeparkart.org/", category="Visual Art")
    venue(db, "No Site")
    assert add_missing(db, TODAY) == 1
    db.commit()
    s = sources(db)[0]
    assert (s.name, s.kind, s.pages, s.venue_id, s.reader) == ("Hyde Park Art Center", SourceKind.museum, ["https://www.hydeparkart.org/"], v.id, "claude")
    assert s.find_link and s.next_check_on == TODAY and s.active and not s.last_status
    assert add_missing(db, TODAY) == 0, "it isn't added twice"


def test_type_comes_from_the_venue(db):
    venue(db, "A", "https://a.example/", category="Music")
    venue(db, "B", "https://b.example/", category="Music", opera=True)
    venue(db, "C", "https://c.example/", category="Theater")
    add_missing(db, TODAY); db.commit()
    assert [s.kind for s in sources(db)] == [SourceKind.music, SourceKind.opera, SourceKind.theater]


def test_left_out_venues(db):
    venue(db, "Hidden", "https://h.example/", hidden=True)
    venue(db, "Pending", "https://p.example/", pending=True)
    venue(db, "Far", "https://f.example/", area=Area.out_of_town)
    venue(db, "Dropped", "https://d.example/", no_source=True)
    assert add_missing(db, TODAY) == 0


def test_a_venue_a_source_already_covers_gets_none(db):
    s = make_source(db)  # Goodman Theatre, reading goodman.example/season
    s.venue.website = "https://www.goodman.example/"
    named = DiscoverySource(name="Steppenwolf", kind=SourceKind.theater, pages=["https://elsewhere.example/x"], venue_name="The Steppenwolf")
    db.add(named); db.commit()
    venue(db, "Steppenwolf", "https://steppenwolf.example/")
    venue(db, "Same Host", "https://goodman.example/")
    assert add_missing(db, TODAY) == 0


def test_another_page_on_a_shared_host_is_its_own_website(db):
    make_source(db)
    venue(db, "A Park", "https://goodman.example/parks/a")
    assert add_missing(db, TODAY) == 1


def test_a_shared_website_is_one_source_counting_all_its_venues(client, admin_token, db):
    today = today_in_chicago()
    a = venue(db, "Cadillac Palace", "https://www.broadway.example/")
    b = venue(db, "Nederlander", "https://broadway.example")
    event(db, a, today); event(db, b, today); event(db, b, today + timedelta(days=1))
    assert add_missing(db, today) == 1
    db.commit()
    got = client.get("/admin/discovery/sources", headers=auth(admin_token)).json()["sources"]
    assert [(s["label"], s["events_year"]) for s in got] == [("Cadillac Palace", 3)]


def test_a_shared_website_stays_one_source_after_the_link_moves(db, pages):  # noqa: F811
    served, _ = pages
    venue(db, "One", "https://shared.example/chicago")
    venue(db, "Two", "https://shared.example/chicago")
    add_missing(db, TODAY); db.commit()
    served["https://shared.example/chicago"] = home(("/calendar", "Calendar"))
    s = sources(db)[0]
    runner.use_calendar_link(s); db.commit()
    assert s.pages == ["https://shared.example/calendar"]
    assert add_missing(db, TODAY) == 0


def test_first_reads_are_spread_out_and_later_ones_queue_behind(db):
    for i in range(FIRST_READS_PER_DAY * 2 + 1):
        venue(db, f"Venue {i:02}", f"https://v{i}.example/")
    add_missing(db, TODAY); db.commit()
    days = [s.next_check_on for s in sources(db)]
    assert [days.count(TODAY + timedelta(days=n)) for n in range(3)] == [FIRST_READS_PER_DAY, FIRST_READS_PER_DAY, 1]
    assert len(runner.due(db, TODAY)) == FIRST_READS_PER_DAY
    venue(db, "Late", "https://late.example/")
    add_missing(db, TODAY); db.commit()
    assert sources(db)[-1].next_check_on == TODAY + timedelta(days=2)


def test_a_deleted_source_stays_deleted(client, admin_token, db):
    a = venue(db, "Gone", "https://gone.example/")
    b = venue(db, "Also Gone", "https://gone.example/")
    add_missing(db, TODAY); db.commit()
    s = sources(db)[0]
    assert client.delete(f"/admin/discovery/sources/{s.id}", headers=auth(admin_token)).status_code == 204
    db.expire_all()
    assert a.no_source and b.no_source
    assert add_missing(db, TODAY) == 0


def test_merging_keeps_a_deleted_source_deleted(client, admin_token, db):
    a = venue(db, "Old Name", no_source=True)
    b = venue(db, "New Name", "https://new.example/")
    r = client.post(f"/venues/{a.id}/merge-into/{b.id}", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert b.no_source


def test_discovery_adds_them_when_it_runs(db, pages):  # noqa: F811
    served, _ = pages
    venue(db, "New Venue", "https://new.example/")
    served["https://new.example/"] = home(("/events", "Events"))
    served["https://new.example/events"] = {"html": "<p>Our season</p>"}
    runner.run(db, FakeClaude([]), TODAY)
    s = sources(db)[0]
    assert s.pages == ["https://new.example/events"] and not s.find_link and s.last_status == "ok"


# ---- the calendar link ---------------------------------------------------------------

def link_on(*links, page="https://www.venue.example/"):
    return calendar_link(home(*links)["html"], page, DiscoverySource(pages=[page]))


def test_finds_the_calendar_link():
    assert link_on(("/about", "About"), ("/calendar", "Calendar")) == "https://www.venue.example/calendar"
    assert link_on(("/visit", "Visit"), ("/whats-on", "What’s on")) == "https://www.venue.example/whats-on"
    assert link_on(("/x", "Upcoming events")) == "https://www.venue.example/x"
    assert link_on(("/season-2026", "See more")) == "https://www.venue.example/season-2026"


def test_the_best_link_wins():
    assert link_on(("/shows", "Shows"), ("/calendar", "Calendar")) == "https://www.venue.example/calendar"
    assert link_on(("/more/events", "More"), ("/x", "Events")) == "https://www.venue.example/x"
    assert link_on(("https://tickets.venue.example/events", "Events")) == "https://tickets.venue.example/events", "a subdomain is the same site"


def test_ignores_what_is_not_a_listing():
    assert link_on(("https://ticketing.example/events", "Events")) is None, "another site"
    assert link_on(("/private-events", "Private events"), ("/rentals", "Host your event")) is None
    assert link_on(("/past", "Past shows"), ("/events/2026/one-show", "One show")) is None
    assert link_on(("#events", "Events"), ("mailto:a@b.example", "Events"), ("/calendar.pdf", "Calendar")) is None
    assert link_on(("/about", "About")) is None
    assert link_on(("/events", "Events"), page="https://www.venue.example/events/") is None, "the page it's on"


def test_no_link_keeps_the_home_page(db, pages):  # noqa: F811
    served, _ = pages
    venue(db, "Plain", "https://plain.example/")
    add_missing(db, TODAY); db.commit()
    served["https://plain.example/"] = home(("/about", "About"))
    s = sources(db)[0]
    runner.use_calendar_link(s)
    assert s.pages == ["https://plain.example/"] and not s.find_link


def test_a_refused_home_page_is_tried_again(db, pages):  # noqa: F811
    served, _ = pages
    venue(db, "Shy", "https://shy.example/")
    add_missing(db, TODAY); db.commit()
    served["https://shy.example/"] = Blocked("the site refused the request (HTTP 403)")
    s = sources(db)[0]
    out = runner.check_source(db, s, None, TODAY)
    assert out.status == "blocked" and s.find_link and s.last_status == "blocked"


def test_an_address_an_admin_set_is_never_replaced(client, admin_token, db, pages):  # noqa: F811
    served, calls = pages
    venue(db, "Fixed", "https://fixed.example/")
    add_missing(db, TODAY); db.commit()
    s = sources(db)[0]
    r = client.patch(f"/admin/discovery/sources/{s.id}", json={"pages": ["https://fixed.example/my-list"]}, headers=auth(admin_token))
    assert r.status_code == 200
    db.expire_all()
    assert not s.find_link
    served["https://fixed.example/my-list"] = home(("/calendar", "Calendar"))
    runner.check_source(db, s, FakeClaude([]), TODAY)
    assert s.pages == ["https://fixed.example/my-list"]
    assert [u for u, _ in calls] == ["https://fixed.example/my-list"]


# ---- the grid's numbers --------------------------------------------------------------

def test_block_states(db):
    s = make_source(db)
    assert state_of(s, 0) == "new"
    s.last_status = "ok"
    assert state_of(s, 0) == "nothing"
    assert state_of(s, 1) == "ok"
    s.last_status = "unchanged"
    assert state_of(s, 1) == "ok", "a page that hasn't changed was read fine"
    for status in ("blocked", "error"):
        s.last_status = status
        assert state_of(s, 5) == "error"
    s.manual = True
    assert state_of(s, 5) == "manual"
    s.active = False
    assert state_of(s, 5) == "off"


def test_size_steps():
    assert [size_of(n) for n in (0, 2, 3, 9, 10, 24, 25, 400)] == [1, 1, 2, 2, 3, 3, 4, 4]


def test_what_counts_toward_a_block(client, admin_token, db):
    today = today_in_chicago()
    s = make_source(db)
    v = s.venue
    event(db, v, today - timedelta(days=30))
    event(db, v, today + timedelta(days=30), title="Upcoming")
    event(db, v, today - timedelta(days=400), end_date=today - timedelta(days=10), title="Ended lately")
    event(db, v, today - timedelta(days=900), open_run=True, title="Open run")
    event(db, v, today - timedelta(days=400), title="Too old")
    event(db, v, today, status=EventStatus.rejected, title="Rejected")
    event(db, v, today, status=EventStatus.withdrawn, title="Withdrawn")
    event(db, v, None, title="No dates")
    got = client.get("/admin/discovery/sources", headers=auth(admin_token)).json()["sources"][0]
    assert (got["events_year"], got["size"]) == (4, 2)


def test_found_and_approved_counts(client, admin_token, db):
    today = today_in_chicago()
    s = make_source(db)
    event(db, s.venue, today)
    live = db.scalar(select(Event))
    db.add(DiscoveredItem(source_id=s.id, key="a", title="A", kind="production", status="approved", event_id=live.id))
    db.add(DiscoveredItem(source_id=s.id, key="b", title="B", kind="production", status="new"))
    s.last_status = "ok"
    db.commit()
    got = client.get("/admin/discovery/sources", headers=auth(admin_token)).json()["sources"][0]
    assert (got["found"], got["approved"], got["state"], got["label"]) == (2, 1, "ok", "Goodman Theatre")


def test_the_venue_url_is_separate_from_the_sourcing_url(client, admin_token, db):
    s = make_source(db)
    s.venue.website = "https://goodman.example/"
    db.commit()
    got = client.get("/admin/discovery/sources", headers=auth(admin_token)).json()["sources"][0]
    assert (got["website"], got["pages"]) == ("https://goodman.example/", ["https://goodman.example/season"])


def test_a_patch_returns_the_same_numbers(client, admin_token, db):
    s = make_source(db)
    r = client.patch(f"/admin/discovery/sources/{s.id}", json={"manual": True}, headers=auth(admin_token))
    assert (r.json()["state"], r.json()["label"]) == ("manual", "Goodman Theatre")


def test_members_get_none_of_this(client, user_token):
    assert client.get("/admin/discovery/sources", headers=auth(user_token)).status_code == 403
