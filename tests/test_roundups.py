"""CC-109: roundup pages at /chicago/: this weekend, free and cheap, closing soon, by kind and by side of town."""
import re
from datetime import date, timedelta
from decimal import Decimal

from app.models import Area, EventRunDate, EventStatus, Region, Venue
from app.roundups import ROUNDUPS, times_on, weekend_of
from app.stats import today_in_chicago
from tests.conftest import auth

TODAY = today_in_chicago()
FRI, SUN = weekend_of(TODAY)


def names(html: str) -> list[str]:
    return re.findall(r'<span class="name">(.*?)</span>', html)


def page(client, slug: str) -> str:
    r = client.get(f"/chicago/{slug}")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "public, max-age=600"
    return r.text


def test_the_weekend_is_this_one_from_friday_on_else_the_next():
    monday = date(2026, 10, 26)
    assert weekend_of(monday) == (date(2026, 10, 30), date(2026, 11, 1))
    assert weekend_of(date(2026, 10, 29)) == (date(2026, 10, 30), date(2026, 11, 1))  # a Thursday
    assert weekend_of(date(2026, 10, 30)) == (date(2026, 10, 30), date(2026, 11, 1))
    assert weekend_of(date(2026, 10, 31)) == (date(2026, 10, 31), date(2026, 11, 1))  # Saturday: from today
    assert weekend_of(date(2026, 11, 1)) == (date(2026, 11, 1), date(2026, 11, 1))


def test_this_weekend_lists_what_is_on_each_day_once(client, make_event, db):
    make_event(title="Long Exhibition", category="Visual Art", venue_name="Art Institute",
               begin=TODAY - timedelta(days=60), end=TODAY + timedelta(days=60))
    make_event(title="Next Month", begin=SUN + timedelta(days=20), end=SUN + timedelta(days=40))
    make_event(title="Closed", begin=TODAY - timedelta(days=30), end=TODAY - timedelta(days=1))
    make_event(title="Pending", status=EventStatus.pending, begin=FRI, end=SUN)
    make_event(title="No Dates Yet", begin=None, end=None)
    weekdays_only = make_event(title="Weeknights", venue_name="Weeknight Bar", begin=TODAY - timedelta(days=3),
                               end=SUN + timedelta(days=30))
    weekdays_only.weekly = {"tue": ["19:30"], "wed": ["19:30"]}
    sunday_show = make_event(title="Sunday Matinee", venue_name="Writers Theatre", begin=TODAY - timedelta(days=3),
                             end=SUN + timedelta(days=30))
    sunday_show.weekly = {"sun": ["14:00"]}
    fixed = make_event(title="Fixed Run Elsewhere", venue_name="Fixed Hall", begin=TODAY - timedelta(days=10),
                       end=SUN + timedelta(days=10))
    fixed.run_dates = [EventRunDate(date=SUN + timedelta(days=3), time="19:30")]
    db.commit()

    html = page(client, "this-weekend")
    listed = names(html)
    assert "Long Exhibition" in listed and "Sunday Matinee" in listed
    for left_out in ("Next Month", "Closed", "Pending", "No Dates Yet", "Weeknights", "Fixed Run Elsewhere"):
        assert left_out not in listed
    assert listed.count("Long Exhibition") == 1  # under its first weekend day only
    assert "<h1>This weekend in Chicago</h1>" in html and "2 events" in html
    sunday = html.split(f'id="h-{SUN.isoformat()}"')[1]
    assert "Sunday Matinee" in sunday and "2 pm" in sunday
    assert "<title>This weekend in Chicago: shows, concerts and exhibitions · Culture Crew</title>" in html


def test_times_on_follows_show_days_and_closed_days(make_event, db):
    ev = make_event(title="Show", begin=FRI, end=SUN)
    ev.weekly = {"sat": ["14:00", "19:30"]}
    db.commit()
    sat = FRI + timedelta(days=(5 - FRI.weekday()) % 7)
    assert times_on(ev, sat) == ["14:00", "19:30"]
    if FRI.weekday() == 4:
        assert times_on(ev, FRI) is None
    museum = make_event(title="Museum", venue_name="Closed Mondays", begin=None, end=None)
    museum.venue.open_hours = {"mon": None, "tue": ["10:00", "17:00"]}
    db.commit()
    monday = TODAY - timedelta(days=TODAY.weekday())
    assert times_on(museum, monday) is None and times_on(museum, monday + timedelta(days=1)) == []


def test_free_and_cheap_is_25_and_under_with_the_venues_usual_price(client, make_event, db):
    make_event(title="Free Gallery", venue_name="Gallery", min_price=Decimal("0"))
    make_event(title="Twenty Five", venue_name="Bar", min_price=Decimal("25"), cost_text="$25")
    make_event(title="Thirty", venue_name="Big Hall", min_price=Decimal("30"))
    make_event(title="No Price", venue_name="Unknown Price Hall")
    usual = make_event(title="Usual Price", venue_name="Cheap Club")
    usual.venue.standard_min_price = Decimal("15")
    usual.venue.standard_cost = "$15 at the door"
    db.commit()
    html = page(client, "free-and-cheap")
    assert set(names(html)) == {"Free Gallery", "Twenty Five", "Usual Price"}
    assert "$15 at the door" in html and ">Free<" in html


def test_closing_soon_is_the_next_two_weeks_soonest_first(client, make_event):
    make_event(title="Later", venue_name="A", end=TODAY + timedelta(days=13))
    make_event(title="Sooner", venue_name="B", end=TODAY + timedelta(days=2))
    make_event(title="Today", venue_name="C", end=TODAY)
    make_event(title="Too Far", venue_name="D", end=TODAY + timedelta(days=14))
    make_event(title="Open Run", venue_name="E", end=None)
    html = page(client, "closing-soon")
    assert names(html) == ["Today", "Sooner", "Later"]
    assert "Last day today" in html and "Closes " in html


def test_kinds_list_opera_under_theater_and_music(client, make_event, db):
    make_event(title="A Play", category="Theater")
    opera = make_event(title="Tosca", category="Music", venue_name="Lyric Opera")
    opera.opera = True
    make_event(title="Quartet", category="Music", venue_name="Hall")
    make_event(title="Prints", category="Visual Art", venue_name="Museum")
    db.commit()
    assert set(names(page(client, "theater"))) == {"A Play", "Tosca"}
    assert set(names(page(client, "music"))) == {"Tosca", "Quartet"}
    assert names(page(client, "visual-art")) == ["Prints"]
    assert 'class="cat cat-music">Opera<' in page(client, "theater")


def test_sides_of_town_go_by_the_venues_region(client, make_event, db):
    north = make_event(title="Uptown Jazz", venue_name="Green Mill", neighborhood="Uptown")
    north.venue.region = Region.north
    burbs = make_event(title="Glencoe Play", venue_name="Writers Theatre", neighborhood="Glencoe, IL", area=Area.suburbs)
    burbs.venue.region = Region.suburbs
    db.commit()
    assert names(page(client, "north-side")) == ["Uptown Jazz"]
    assert names(page(client, "suburbs")) == ["Glencoe Play"]


def test_soonest_on_first(client, make_event):
    make_event(title="Opens Later", venue_name="A", begin=TODAY + timedelta(days=9), end=TODAY + timedelta(days=30))
    make_event(title="On Now Long", venue_name="B", begin=TODAY - timedelta(days=9), end=TODAY + timedelta(days=60))
    make_event(title="On Now Short", venue_name="C", begin=TODAY - timedelta(days=9), end=TODAY + timedelta(days=5))
    make_event(title="Opens Soon", venue_name="D", begin=TODAY + timedelta(days=2), end=TODAY + timedelta(days=30))
    assert names(page(client, "theater")) == ["On Now Short", "On Now Long", "Opens Soon", "Opens Later"]


def test_an_empty_roundup_says_so_and_links_to_the_catalog(client):
    html = page(client, "south-side")
    assert "Nothing on the South Side is on the list right now" in html and "/#catalog" in html
    assert names(html) == []


def test_each_page_links_its_events_and_the_other_roundups(client, make_event):
    ev = make_event(title="Hamlet")
    html = page(client, "theater")
    assert f'/e/{ev.id}-hamlet"' in html
    for r in ROUNDUPS:
        assert f'/chicago/{r.slug}"' in html
    assert 'rel="canonical" href="' in html and "/chicago/theater" in html and 'property="og:title"' in html


def test_index_redirect_and_unknown(client, make_event):
    make_event(title="Hamlet")
    html = client.get("/chicago/").text
    assert "<h1>What’s on in Chicago</h1>" in html and "Theater</a> <span class=\"count\">· 1 event" in html
    r = client.get("/chicago", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/chicago/"
    r = client.get("/chicago/nope")
    assert r.status_code == 404 and "noindex" in r.text


def test_admins_edit_the_intro_and_can_go_back_to_the_default(client, admin_token, user_token):
    assert client.get("/admin/roundups", headers=auth(user_token)).status_code == 403
    rows = client.get("/admin/roundups", headers=auth(admin_token)).json()
    assert [r["slug"] for r in rows] == [r.slug for r in ROUNDUPS] and not any(r["custom"] for r in rows)

    r = client.put("/admin/roundups/theater", json={"intro": "Our <b>favourite</b> stages.\n\nSecond paragraph."},
                   headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["custom"] and r.json()["intro"].startswith("Our")
    html = page(client, "theater")
    assert "<p>Our &lt;b&gt;favourite&lt;/b&gt; stages.</p><p>Second paragraph.</p>" in html

    r = client.put("/admin/roundups/theater", json={"intro": "  "}, headers=auth(admin_token))
    assert not r.json()["custom"] and r.json()["intro"] == r.json()["default_intro"]
    assert client.put("/admin/roundups/nope", json={"intro": "x"}, headers=auth(admin_token)).status_code == 404
    assert client.put("/admin/roundups/theater", json={"intro": "x" * 601}, headers=auth(admin_token)).status_code == 422


def test_sitemap_lists_the_index_and_roundups_with_events(client, make_event):
    make_event(title="Hamlet", category="Theater")
    xml = client.get("/sitemap.xml").text
    assert "/chicago/</loc>" in xml and "/chicago/theater</loc>" in xml
    assert "/chicago/visual-art</loc>" not in xml  # nothing there yet


def test_a_busy_day_cannot_crowd_out_the_rest_of_the_weekend(client, make_event, db, monkeypatch):
    if FRI == SUN:
        return  # on a Sunday the weekend is one day
    monkeypatch.setattr("app.roundups.DAY_MAX", 1)
    make_event(title="First Day A", venue_name="A", begin=FRI, end=FRI)
    make_event(title="First Day B", venue_name="B", begin=FRI, end=FRI)
    make_event(title="Last Day", venue_name="C", begin=SUN, end=SUN)
    html = page(client, "this-weekend")
    assert len(names(html)) == 2 and "Last Day" in names(html)
    assert "That’s 2 of 3." in html and f'href="#{SUN.strftime("%a").lower()}"' in html
