"""CC-33: the catalog filters, sorts and pages on the server."""
from datetime import date, timedelta
from decimal import Decimal

from app.models import Area, Event, EventStatus

from .conftest import auth

T = date.today()


def titles(client, **params):
    r = client.get("/events", params={"include_past": "true", "from": T.isoformat(), **params})
    assert r.status_code == 200, r.text
    return [e["title"] for e in r.json()["items"]]


def test_sort_orders(client, make_event):
    make_event("The Zebra", end=T + timedelta(days=3), min_price=Decimal("50"))
    make_event("apple", end=T + timedelta(days=10), min_price=Decimal("10"))
    make_event("A Midsummer", end=None, min_price=None)  # open run, price not posted
    make_event("Opening Soon", begin=T + timedelta(days=5), end=T + timedelta(days=40), min_price=Decimal("0"))

    assert titles(client, sort="closing") == ["The Zebra", "apple", "Opening Soon", "A Midsummer"]  # no end date last
    assert titles(client, sort="az") == ["apple", "A Midsummer", "Opening Soon", "The Zebra"]  # "The"/"A" ignored
    assert titles(client, sort="price") == ["Opening Soon", "apple", "The Zebra", "A Midsummer"]  # unposted last
    assert titles(client, sort="opening")[0] == "Opening Soon"  # not open yet comes first
    assert titles(client, sort="opening")[1:] == ["The Zebra", "apple", "A Midsummer"]  # then by closing


def test_neighborhood_filter_and_search(client, make_event):
    make_event("Bug", venue_name="Steppenwolf", neighborhood="Lincoln Park")
    make_event("Hamlet", venue_name="Goodman Theatre", neighborhood="Loop")
    make_event("Gallery show", venue_name="Gallery", neighborhood="Evanston, IL", area=Area.suburbs)
    assert titles(client, hood=["Loop", "Lincoln Park"], sort="az") == ["Bug", "Hamlet"]
    assert titles(client, q="lincoln") == ["Bug"]  # search covers neighborhoods too
    assert titles(client, q="evanston") == ["Gallery show"]


def test_already_closed_uses_the_callers_date(client, make_event):
    make_event("Closed yesterday", begin=T - timedelta(days=9), end=T - timedelta(days=1))
    make_event("Closes today", begin=T - timedelta(days=9), end=T)
    r = client.get("/events", params={"ended_before": T.isoformat(), "sort": "closing"})
    assert [e["title"] for e in r.json()["items"]] == ["Closed yesterday"]
    # a caller whose local date is still yesterday sees "Closes today" as running, and nothing closed
    yesterday = (T - timedelta(days=1)).isoformat()
    assert "Closes today" in titles(client, **{"from": yesterday})
    assert client.get("/events", params={"ended_before": yesterday}).json()["total"] == 0


def test_paging_reports_the_full_total(client, make_event):
    for i in range(5):
        make_event(f"Show {i}", end=T + timedelta(days=i + 1))
    r = client.get("/events", params={"sort": "closing", "limit": 2, "offset": 2}).json()
    assert r["total"] == 5 and [e["title"] for e in r["items"]] == ["Show 2", "Show 3"]


def test_facets(client, make_event):
    make_event("Hamlet", neighborhood="Loop")
    make_event("Bug", venue_name="Steppenwolf", neighborhood="Lincoln Park")
    make_event("Joke night", venue_name="Annoyance", neighborhood="Lakeview", category="Comedy")
    make_event("Old show", venue_name="Old Venue", neighborhood="Pilsen", begin=T - timedelta(days=60),
               end=T - timedelta(days=30))
    make_event("Hidden", venue_name="Secret", neighborhood="Hidden Hood", status=EventStatus.pending)
    make_event("No hood", venue_name="Somewhere", neighborhood="")

    f = client.get("/events/facets", params={"today": T.isoformat()}).json()
    assert f["categories"] == ["Theater", "Comedy", "Music", "Visual Art", "Educational"]  # CC-125: always the five
    assert [n["name"] for n in f["neighborhoods"]] == ["Lakeview", "Lincoln Park", "Loop", "Pilsen"]  # live only, no blanks
    assert f["neighborhoods"][0]["area"] == "city"
    assert (f["current"], f["total"]) == (4, 5)  # "Old show" has closed


def test_lookup_by_id_or_old_site_id(client, make_event, db):
    a = make_event("Hamlet")
    b = make_event("Bug", venue_name="Steppenwolf")
    b.source_id = "abc123"
    hidden = make_event("Pending", status=EventStatus.pending)
    db.commit()
    r = client.get("/events/lookup", params=[("ids", str(a.id)), ("ids", "abc123"), ("ids", str(hidden.id)), ("ids", "nope")])
    assert r.status_code == 200 and [e["title"] for e in r.json()] == ["Hamlet", "Bug"]  # pending and unknown left out
    assert client.get("/events/lookup", params=[("ids", str(i)) for i in range(11)]).status_code == 422  # at most 10
    assert client.get("/events/facets").status_code == 200  # not mistaken for an event id


def test_sort_titles():
    from app.models import sort_title
    assert [sort_title(t) for t in ("The Lion King", "A Midsummer", "(Re)arranging", "... before the dawn",
                                    "Theater of Dreams", "4000 Miles", "???")] == \
        ["lion king", "midsummer", "re)arranging", "before the dawn", "theater of dreams", "4000 miles", "???"]


def test_sort_title_follows_title_changes(client, admin_token, make_event, db):
    ev = make_event("The Zebra")
    assert ev.sort_title == "zebra"
    client.patch(f"/events/{ev.id}", json={"title": "An Aardvark"}, headers=auth(admin_token))
    db.expire_all()
    assert db.get(Event, ev.id).sort_title == "aardvark"
