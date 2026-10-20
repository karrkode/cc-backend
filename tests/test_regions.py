"""CC-73: the side of town each venue is in, for Plan an outing and the catalog."""
from sqlalchemy import select

from app.areas import region_of
from app.models import Area, Region, Venue

from .conftest import auth


def test_region_of():
    assert region_of("Loop") == Region.downtown and region_of("West Loop") == Region.downtown
    assert region_of("Rogers park") == Region.north  # any capitals
    assert region_of("Wicker Park") == Region.west and region_of("Hyde Park") == Region.south
    assert region_of("Evanston, IL") == Region.suburbs and region_of("Niles") == Region.suburbs
    assert region_of("Milwaukee, WI") == Region.out_of_town and region_of("Rockford, IL") == Region.out_of_town
    assert region_of("Somewhere New") is None  # a city neighborhood not in the list: an admin places it
    assert region_of("Somewhere New", Area.suburbs) == Region.suburbs


def test_new_venues_get_a_region_and_the_catalog_filters_by_it(client, admin_token, make_event, db):
    h = auth(admin_token)
    base = {"category": "Theater", "publish": "now"}
    for title, hood in (("Downtown show", "Loop"), ("North show", "Lakeview"), ("Burbs show", "Evanston, IL"), ("Odd show", "Nowhereville")):
        assert client.post("/events", json={**base, "title": title, "venue": {"name": title + " Hall", "neighborhood": hood}}, headers=h).status_code == 201
    regions = {v.neighborhood: v.region for v in db.scalars(select(Venue))}
    assert regions == {"Loop": Region.downtown, "Lakeview": Region.north, "Evanston, IL": Region.suburbs, "Nowhereville": None}
    titles = lambda **p: sorted(e["title"] for e in client.get("/events", params={"include_past": "true", **p}).json()["items"])
    assert titles(region="downtown") == ["Downtown show"]
    assert titles(region=["north", "suburbs"]) == ["Burbs show", "North show"]
    hoods = {n["name"]: n["region"] for n in client.get("/events/facets").json()["neighborhoods"]}
    assert hoods["Lakeview"] == "north" and hoods["Nowhereville"] is None


def test_admins_set_or_fix_a_region(client, admin_token, db):
    h = auth(admin_token)
    db.add(Venue(name="New Hall", neighborhood="Nowhereville", area=Area.city)); db.commit()
    v = db.scalar(select(Venue))
    listed = client.get("/venues/manage", headers=h).json()
    assert listed[0]["region"] is None  # the Venues page lists it to fix
    assert client.patch(f"/venues/{v.id}", json={"region": "west"}, headers=h).json()["region"] == "west"
    # moving the venue works the region out again, unless one is given
    assert client.patch(f"/venues/{v.id}", json={"neighborhood": "Hyde Park"}, headers=h).json()["region"] == "south"
    assert client.patch(f"/venues/{v.id}", json={"neighborhood": "Loop", "region": "north"}, headers=h).json()["region"] == "north"
    assert client.patch(f"/venues/{v.id}", json={"region": None}, headers=h).json()["region"] is None
