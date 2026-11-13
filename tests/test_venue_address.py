"""CC-218: venues have a public street address. Admins type it or confirm a suggestion; nothing fills it in by itself."""
import json
from datetime import timedelta

import pytest

from app.addresses import found_address, full_address, map_url, normalize_address
from app.discovery import store
from app.discovery.jsonld import event_addresses
from app.models import Area, DiscoverySource, SourceKind, Venue, VenueAddressSuggestion
from app.stats import today_in_chicago

from .conftest import auth

STREET, CITY, ZIP = "100 W Example St", "Chicago", "60601"
WHOLE = {"address": STREET, "city": CITY, "zip_code": ZIP}


def page(*events) -> str:
    return "".join(f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in events)


def event(place="Hall", address=None):
    address = {"@type": "PostalAddress", "streetAddress": STREET, "addressLocality": CITY, "postalCode": ZIP} if address is None else address
    return {"@type": "TheaterEvent", "name": "A Show", "location": {"@type": "Place", "name": place, "address": address}}


def source_at(db, venue, name="Hall events"):
    s = DiscoverySource(name=name, kind=SourceKind.theater, pages=["https://hall.example/events"], venue_id=venue.id, venue_name=venue.name)
    db.add(s)
    db.commit()
    return s


def venue_of(db, make_event, **kw):
    return db.get(Venue, make_event(**kw).venue_id)


@pytest.mark.parametrize("raw, stored", [("  100  W\nExample St ", STREET), ("", ""), ("Millennium Park", "Millennium Park")])
def test_addresses_are_tidied(raw, stored):
    assert normalize_address(raw) == stored


@pytest.mark.parametrize("bad", ["<b>100 W Example</b>", "https://x.example/", "100 W\x00Example", "1" * 201])
def test_what_cant_be_an_address_is_refused(client, admin_token, make_event, bad):
    r = client.patch(f"/venues/{make_event().venue_id}", json={"address": bad}, headers=auth(admin_token))
    assert r.status_code == 422


def test_the_whole_address_reads_as_one_line():
    assert full_address(STREET, CITY, ZIP, Area.city, "Loop") == f"{STREET}, Chicago, IL 60601"
    assert full_address(STREET, "Racine", "53403", Area.suburbs, "Racine, WI") == f"{STREET}, Racine, WI 53403"
    assert full_address(STREET, "", "", Area.city, "Loop") == f"{STREET}, Chicago, IL"  # only ever set straight in the database
    assert full_address("", CITY, ZIP, Area.city, "Loop") == "" and map_url("") == ""
    assert map_url("1 A St, Chicago, IL").endswith("query=1%20A%20St%2C%20Chicago%2C%20IL")


@pytest.mark.parametrize("field, bad", [("zip_code", "6060"), ("zip_code", "ABCDE"), ("zip_code", "606011"), ("city", "606 Town"), ("city", "<b>Chicago</b>")])
def test_a_bad_city_or_zip_is_refused(client, admin_token, make_event, field, bad):
    r = client.patch(f"/venues/{make_event().venue_id}", json={**WHOLE, field: bad}, headers=auth(admin_token))
    assert r.status_code == 422


@pytest.mark.parametrize("left_out", ["address", "city", "zip_code"])
def test_an_address_is_all_three_or_none(client, db, admin_token, make_event, left_out):
    vid, h = make_event().venue_id, auth(admin_token)
    assert client.patch(f"/venues/{vid}", json={k: v for k, v in WHOLE.items() if k != left_out}, headers=h).status_code == 422
    assert client.patch(f"/venues/{vid}", json=WHOLE, headers=h).status_code == 200
    assert client.patch(f"/venues/{vid}", json={left_out: ""}, headers=h).status_code == 422  # can't lose just one
    assert client.patch(f"/venues/{vid}", json={"zip_code": "60602"}, headers=h).json()["zip_code"] == "60602"
    cleared = client.patch(f"/venues/{vid}", json={"address": "", "city": "", "zip_code": ""}, headers=h).json()
    assert (cleared["address"], cleared["city"], cleared["zip_code"]) == ("", "", "")


def test_an_admin_sets_it_and_everyone_sees_it(client, db, admin_token, user_token, make_event):
    ev = make_event(begin=today_in_chicago())
    assert client.patch(f"/venues/{ev.venue_id}", json=WHOLE, headers=auth(user_token)).status_code == 403
    r = client.patch(f"/venues/{ev.venue_id}", json=WHOLE, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["address"] == STREET
    venue = client.get(f"/events/{ev.id}").json()["venue"]  # signed out
    assert venue["address"] == STREET and venue["full_address"] == f"{STREET}, Chicago, IL 60601"
    assert venue["map_url"].startswith("https://www.google.com/maps/search/?api=1&query=100%20W%20Example%20St")


def test_the_public_page_and_event_data_have_it_only_when_set(client, db, make_event):
    today = today_in_chicago()
    ev = make_event(title="Open Show", begin=today, end=today + timedelta(days=3))
    text = client.get(f"/e/{ev.id}-open-show").text
    assert "<dt>Address" not in text and "google.com/maps" not in text and "streetAddress" not in text
    venue = db.get(Venue, ev.venue_id)
    venue.address, venue.city, venue.zip_code = "1 <Odd> & Co St", "Oak Park", ZIP  # escaped, whatever is stored
    db.commit()
    text = client.get(f"/e/{ev.id}-open-show").text
    assert "1 &lt;Odd&gt; &amp; Co St, Oak Park, IL 60601" in text and "<Odd>" not in text.split("ld+json")[0]
    assert "google.com/maps/search" in text and '"streetAddress": "1 \\u003cOdd\\u003e \\u0026 Co St"' in text  # CC-199: escaped inside the script block
    assert '"postalCode": "60601"' in text and '"addressLocality": "Oak Park"' in text


def test_an_admin_adding_a_venue_directly_must_give_the_address(client, admin_token, user_token):
    h, body = auth(admin_token), {"name": "New Hall", "neighborhood": "Loop"}
    assert client.post("/venues", json=body, headers=h).status_code == 422
    for left_out in WHOLE:
        assert client.post("/venues", json={**body, **{k: v for k, v in WHOLE.items() if k != left_out}}, headers=h).status_code == 422
    assert client.post("/venues", json={**body, **WHOLE, "address": "   "}, headers=h).status_code == 422
    assert client.post("/venues", json={**body, **WHOLE}, headers=auth(user_token)).status_code == 403
    r = client.post("/venues", json={**body, **WHOLE, "address": f" {STREET} "}, headers=h)
    assert r.status_code == 201 and (r.json()["address"], r.json()["city"], r.json()["zip_code"]) == (STREET, CITY, ZIP)


def test_a_venue_added_along_the_way_needs_no_address(client, admin_token, user_token):
    body = {"name": "Found Hall", "neighborhood": "Loop"}
    assert client.post("/venues/from-event", json=body, headers=auth(user_token)).status_code == 403
    r = client.post("/venues/from-event", json=body, headers=auth(admin_token))
    assert r.status_code == 201 and r.json()["address"] == "" and r.json()["map_url"] == ""
    assert client.post("/venues/from-event", json=body, headers=auth(admin_token)).status_code == 409


def test_merging_keeps_the_address(client, db, admin_token, make_event):
    a, b = make_event(venue_name="One").venue_id, make_event(venue_name="Two").venue_id
    venue = db.get(Venue, a)
    venue.address, venue.city, venue.zip_code = STREET, CITY, ZIP
    db.commit()
    assert client.post(f"/venues/{a}/merge-into/{b}", headers=auth(admin_token)).status_code == 200
    db.expire_all()
    kept = db.get(Venue, b)
    assert (kept.address, kept.city, kept.zip_code) == (STREET, CITY, ZIP)


@pytest.mark.parametrize("address, found", [
    ({"streetAddress": STREET, "addressLocality": CITY, "postalCode": ZIP}, (STREET, CITY, ZIP)),
    ({"streetAddress": STREET, "addressLocality": CITY}, (STREET, CITY, "")),
    ({"streetAddress": f"{STREET}, Chicago, IL 60601", "addressLocality": CITY}, (STREET, CITY, ZIP)),
    ({"streetAddress": f"{STREET}, Chicago, IL 60601"}, (STREET, CITY, ZIP)),
    ([{"streetAddress": STREET, "postalCode": ZIP}], (STREET, "", ZIP)),
    (f"{STREET}, Chicago, IL 60601", (STREET, CITY, ZIP)),
    (f"{STREET}, Chicago, IL 60601, USA", (STREET, CITY, ZIP)),
    (f"{STREET}, Chicago", (STREET, CITY, "")),
    (STREET, (STREET, "", "")),
])
def test_the_address_is_read_from_a_pages_event_data(address, found):
    assert event_addresses(page(event(address=address))) == [("Hall", *found)]


@pytest.mark.parametrize("html", ["", "<p>No data</p>", page({"@type": "TheaterEvent", "name": "A Show"}),
                                  page(event(address={"addressLocality": "Chicago"})), page({"@type": "Place", "address": {"streetAddress": STREET}})])
def test_pages_without_a_street_give_none(html):
    assert event_addresses(html) == []


@pytest.mark.parametrize("raw", ["See website", "TBA", "12", "<script>1 st</script>", "https://x.example/1"])
def test_junk_isnt_worth_suggesting(raw):
    assert found_address(raw, CITY, ZIP) is None


def test_a_bad_city_or_zip_on_a_page_is_left_blank():
    assert found_address(STREET, "<b>", "n/a") == (STREET, "", "")


def test_discovery_only_suggests(client, db, admin_token, make_event):
    venue = venue_of(db, make_event, venue_name="Hall")
    source = source_at(db, venue)
    store.note_addresses(db, source, page(event()))
    store.note_addresses(db, source, page(event()))  # read again: still one
    db.commit()
    db.expire_all()
    assert db.get(Venue, venue.id).address == ""
    assert client.get("/venues/address-suggestions").status_code == 401
    assert client.get("/venues/address-suggestions", headers=auth(admin_token)).json() == [
        {"venue_id": venue.id, "venue_name": "Hall", "neighborhood": venue.neighborhood, "address": STREET, "city": CITY,
         "zip_code": ZIP, "source": "Hall events"}]


def test_a_venue_with_an_address_gets_no_suggestion_and_keeps_it(client, db, admin_token, make_event):
    venue = venue_of(db, make_event, venue_name="Hall")
    venue.address = "1 Admin Way"
    source = source_at(db, venue)
    store.note_addresses(db, source, page(event()))
    db.commit()
    db.expire_all()
    assert db.get(Venue, venue.id).address == "1 Admin Way"
    assert db.query(VenueAddressSuggestion).count() == 0


def test_a_location_that_isnt_a_known_venue_suggests_nothing(db, make_event):
    source = source_at(db, venue_of(db, make_event, venue_name="Hall"))
    store.note_addresses(db, source, page(event(place="A Stage Elsewhere")))
    db.commit()
    assert db.query(VenueAddressSuggestion).count() == 0


def test_another_known_venue_named_on_the_page_gets_the_suggestion(db, make_event):
    source = source_at(db, venue_of(db, make_event, venue_name="Hall"))
    other = venue_of(db, make_event, venue_name="Other Place")
    store.note_addresses(db, source, page(event(place="Other Place")))
    db.commit()
    assert [s.venue_id for s in db.query(VenueAddressSuggestion)] == [other.id]


def test_confirm_edit_and_never_replace(client, db, admin_token, make_event):
    venue = venue_of(db, make_event, venue_name="Hall")
    store.note_addresses(db, source_at(db, venue), page(event()))
    db.commit()
    h = auth(admin_token)
    assert client.post(f"/venues/{venue.id}/address", json={"address": STREET}, headers=h).status_code == 422  # all three
    r = client.post(f"/venues/{venue.id}/address", json={**WHOLE, "address": "100 W Example Street"}, headers=h)  # edited
    assert r.status_code == 200 and r.json()["address"] == "100 W Example Street" and r.json()["zip_code"] == ZIP
    assert client.get("/venues/address-suggestions", headers=h).json() == []
    assert client.post(f"/venues/{venue.id}/address", json=WHOLE, headers=h).status_code == 409


def test_a_later_page_fills_in_what_an_earlier_one_didnt(client, db, admin_token, make_event):
    venue = venue_of(db, make_event, venue_name="Hall", neighborhood="Loop")
    source = source_at(db, venue)
    store.note_addresses(db, source, page(event(address={"streetAddress": STREET})))
    db.commit()
    got = client.get("/venues/address-suggestions", headers=auth(admin_token)).json()[0]
    assert (got["city"], got["zip_code"]) == (CITY, "")  # the venue's town stands in; the ZIP is the admin's to add
    store.note_addresses(db, source, page(event()))
    db.commit()
    assert client.get("/venues/address-suggestions", headers=auth(admin_token)).json()[0]["zip_code"] == ZIP


def test_dismissed_suggestions_stay_away(client, db, admin_token, make_event):
    venue = venue_of(db, make_event, venue_name="Hall")
    source = source_at(db, venue)
    store.note_addresses(db, source, page(event()))
    db.commit()
    h = auth(admin_token)
    assert client.post(f"/venues/{venue.id}/address/dismiss", json={"address": STREET}, headers=h).status_code == 204
    assert client.post(f"/venues/{venue.id}/address/dismiss", json={"address": STREET}, headers=h).status_code == 204
    store.note_addresses(db, source, page(event()))
    db.commit()
    assert client.get("/venues/address-suggestions", headers=h).json() == []


def test_only_admins_confirm_or_dismiss(client, user_token, make_event):
    vid = make_event().venue_id
    for path in (f"/venues/{vid}/address", f"/venues/{vid}/address/dismiss"):
        assert client.post(path, json=WHOLE, headers=auth(user_token)).status_code == 403
